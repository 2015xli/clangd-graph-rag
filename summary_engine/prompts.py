from typing import Optional
import logging
from dataclasses import dataclass

# Set up logging for this module
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

@dataclass(frozen=True, slots=True)
class PromptEnv:
    """
    Wraps the environment context for a prompt.
    """
    project_name: str
    project_info: str
    file_path: str
    node_scope: str
    node_kind: str
    node_name: str

class PromptManager:
    """
    Manages the prompt templates used for generating code analyses and summaries.
    """
    def __init__(self):
        pass

    def _apply_env_header(self, env: PromptEnv, prompt: str) -> str:
        """Prepends the standard context header to a prompt."""
        header = (
            f"The C/C++ code snippet below for '{env.node_kind} {env.node_name}' is provided within the following context:\n"
            f"- Project Name: {env.project_name}\n"
            f"- Project Background: {env.project_info or '(N/A)'}\n"
            f"- File Relative Path: {env.file_path or '(N/A)'}\n"
            f"- Code Lexical Scope: {env.node_scope or '(N/A)'}\n"
            f"{'-' * 50}\n\n"
        )
        return header + prompt

    def get_code_analysis_prompt(self, env: PromptEnv, chunk: str, is_first_chunk: bool, is_last_chunk: bool, running_summary: str = "") -> str:
        """
        Returns the prompt for individual code analysis (Pass 1).
        Handles first, middle, and last chunks for iterative summarization of large functions.
        """
        if is_first_chunk:
            if is_last_chunk:
                # Single chunk function
                prompt = (
                    f"## Goal:\n Summarize the purpose of this C/C++ '{env.node_kind} {env.node_name}' based on its code below. " 
                    f"Your summary should start with 'The {env.node_kind} \'{env.node_name}\' ...'. "
                    f"Don't respond with your reasoning process, but only give the summary.:\n\n```cpp\n{chunk}```"
                )
            else:
                # First chunk of a multi-chunk function
                prompt = (
                    f"## Goal:\n Summarize this C/C++ '{env.node_kind} {env.node_name}', which is the beginning of a larger function/method. " 
                    f"Your summary should start with 'The {env.node_kind} \'{env.node_name}\' ...'. "
                    f"Don't respond with your reasoning process, but only give the summary.:\n\n```cpp\n{chunk}```"
                )
        else:
            # Subsequent chunks
            position_prompt = "This is the end of the function body." if is_last_chunk else "The function body continues after this code."
            prompt = (
                f"The summary of the first part of a large function/method '{env.node_kind} {env.node_name}' so far is: \n'{running_summary}'\n\n" 
                f"Here is the next part of the code:\n```cpp\n{chunk}```\n\n" 
                f"{position_prompt}\n\n"
                f"Please provide a new summary that combines the previous summary with this new code, starting with 'The {env.node_kind} \'{env.node_name}\' ...'. "
                f"Don't respond with your reasoning process, but only give the summary."
            )
        return self._apply_env_header(env, prompt)

    def get_contextual_function_prompt(self, env: PromptEnv, code_analysis: str, caller_analyses: str, callee_analyses: str) -> str:
        """
        Returns the prompt for contextual function summarization (Pass 2, single pass).
        Combines the function's own analysis with its callers and callees.
        """
        caller_text = "\n Another caller: ".join([s for s in caller_analyses if s]) or "none"
        callee_text = "\n Another callee: ".join([s for s in callee_analyses if s]) or "none"
        prompt = (
            f"## Summarize '{env.node_kind} {env.node_name}' with the following invocation context:\n"
            f"### Its code analysis\n The C/C++ '{env.node_kind} {env.node_name}' is analyzed as: '{code_analysis}'.\n"
            f"### Its callers\n It is called by functions with these responsibilities: [{caller_text}].\n"
            f"### Its callees\n It calls other functions to do the following: [{callee_text}].\n\n"
            f"## Goal\n Based on this context, what is the high-level purpose of this function/method in the overall system? "
            f"Describe it in concise sentences, starting with 'The {env.node_kind} \'{env.node_name}\' ...'."
            f"Don't respond with your reasoning process, but only give the summary."
        )
        return self._apply_env_header(env, prompt)

    def get_iterative_caller_prompt_template(self) -> str:
        """Returns the template for iterative caller summarization."""
        return (
            "The function being summarized has this purpose: {running_summary}. "
            "It is used by other functions with the following responsibilities: {relation_summaries_chunk}. "
            "Describe the main function's role in relation to its callers, starting with 'The {env.node_kind} \'{env.node_name}\' ...'."
        )

    def get_iterative_callee_prompt_template(self) -> str:
        """Returns the template for iterative callee summarization."""
        return (
            "So far, a function's role is summarized as: {running_summary}. "
            "It accomplishes this by calling other functions for these purposes: {relation_summaries_chunk}. "
            "Provide a final, comprehensive summary of the function's overall purpose, starting with 'The {env.node_kind} \'{env.node_name}\' ...'."
        )

    def get_class_manifest_summary_prompt(self, env: PromptEnv, class_name: str, kind: str, template_context: str, definition_context: str, parent_text: str, field_text_prompt: str, method_text: str) -> str:
        """
        Returns a detailed prompt for class summarization using a manifest approach.
        Includes physical definition/origin, template metadata, and member inventory.
        """
        prompt = (
            f"## Summarize the {kind} named '{class_name}' with following context.\n\n"
            f"### Context:\n"
            f"- **Kind**: {kind}\n"
            f"{template_context}\n"
            f"- **Inheritance**: {parent_text or 'None'}\n\n"
            f"### Definition/Origin:\n"
            f"{definition_context}\n\n"
            f"### Member Inventory:\n"
            f"- **Fields**: {field_text_prompt or 'None'}\n"
            f"- **Methods**: {method_text or 'None'}\n\n"
            f"## Goal:\n"
            f"Provide a summary of the {kind}'s primary responsibility and architectural role, starting with 'The {env.node_kind} \'{env.node_name}\' ...' "
            f"Do not respond with your reasoning process, only the summary."
        )
        return self._apply_env_header(env, prompt)

    def get_scc_analysis_prompt(self, env: PromptEnv, combined_bodies: str) -> str:
        """
        Returns a prompt for collective analysis of recursive class inheritance.
        """
        prompt = (
            f"The following C++ classes form a recursive inheritance structure (a cycle or mutual recursion).\n\n"
            f"### Source Code Definitions:\n"
            f"{combined_bodies}\n\n"
            f"### Goal:\n"
            f"Analyze these definitions together and provide a concise summary of the collective logic of this recursive structure. "
            f"Identify the termination condition (if visible) and the primary purpose of this recursion. "
            f"Do not respond with your reasoning process, only the collective summary."
        )
        return self._apply_env_header(env, prompt)

    def get_iterative_class_inheritance_prompt_template(self) -> str:
        """Returns the template for iterative class inheritance summarization."""
        return (
            "A class is described as: {running_summary}. "
            "It inherits from parent classes with these responsibilities: {relation_summaries_chunk}. "
            "Refine the summary to include the role of its inheritance, starting with 'The {env.node_kind} \'{env.node_name}\' ...'"
        )

    def get_iterative_class_method_prompt_template(self) -> str:
        """Returns the template for iterative class method summarization."""
        return (
            "So far, a class's role is summarized as: {running_summary}. "
            "It implements methods to perform these functions: {relation_summaries_chunk}. "
            "Provide a final, comprehensive summary of the class's overall purpose, "
            "starting with 'The {env.node_kind} \'{env.node_name}\' ...'."
        )

    def get_file_manifest_summary_prompt(self, env: PromptEnv, file_name: str, includes_text: str, inventory_text: str) -> str:
        """
        Returns a detailed prompt for file summarization using a manifest approach.
        """
        prompt = (
            f"## Summarize the purpose of the source file '{file_name}' with following context.\n\n"
            f"### Context:\n"
            f"- **Includes**: {includes_text or 'None'}\n"
            f"- **Definitions/Declarations**:\n{inventory_text or 'None'}\n\n"
            f"## Goal:\n"
            f"Provide a concise summary of the file's primary responsibility, its role, and architecture in the project, "
            f"starting with 'The {env.node_kind} \'{env.node_name}\' ...'." 
            f"Do not respond with your reasoning process, only the summary."
        )
        return self._apply_env_header(env, prompt)

    def get_folder_summary_prompt(self, env: PromptEnv, folder_name: str, child_summaries_text: str) -> str:
        """
        Returns the prompt for folder summarization (Pass 5).
        Rolls up summaries of files and subfolders.
        """
        prompt = (
            f"A folder named '{folder_name}' contains the following components: [{child_summaries_text}]. "
            f"What is this folder's collective role and the module's architecture in the project?"
        )
        return self._apply_env_header(env, prompt)

    def get_folder_manifest_summary_prompt(self, env: PromptEnv, folder_name: str, inventory_text: str) -> str:
        """
        Returns a detailed prompt for folder summarization using a manifest approach.
        """
        prompt = (
            f"## Summarize the collective purpose of the folder '{folder_name}' with following context.\n\n"
            f"### Components:\n"
            f"{inventory_text or 'This folder is empty or contains no recognized source files.'}\n\n"
            f"### Goal:\n"
            f"Provide a summary of this folder's role in the project's organization, starting with 'The {env.node_kind} \'{env.node_name}\' ...'." 
            f"Do not respond with your reasoning process, only the summary."
        )
        return self._apply_env_header(env, prompt)

    def get_project_summary_prompt(self, env: PromptEnv, child_summaries_text: str) -> str:
        """
        Returns the prompt for project summarization (Pass 5).
        Final top-level roll-up.
        """
        prompt = (
            f"A software project contains the following top-level components: [{child_summaries_text}]. "
            f"What is the overall purpose and architecture of this project?"
        )
        return self._apply_env_header(env, prompt)

    def get_namespace_summary_prompt(self, env: PromptEnv, namespace_name: str, child_summaries_text: str) -> str:
        """
        Returns the prompt for namespace summarization (Pass 4).
        Rolls up entities contained within the C++ namespace.
        """
        prompt = (
            f"A C++ namespace named '{namespace_name}' contains the following components: [{child_summaries_text}]. "
            f"What is this namespace's collective role and purpose?"
        )
        return self._apply_env_header(env, prompt)

    def get_iterative_parent_children_prompt(self, relation_name: str, entity_name: Optional[str] = None) -> str:
        """
        Returns the template for iterative parent children summarization.
        Used when the number of children is too large for a single prompt.
        """
        label = relation_name.split('_')[0]
        return (
            f"The {label} '{entity_name}' in this C/C++ software is summarized as: {{running_summary}}\n"
            f"It also contains the following major components: {{relation_summaries_chunk}}\n"
            f"Provide a new, comprehensive summary of the {label} '{entity_name}' on its role and overall purpose,"
            f"starting with 'The {label} {entity_name} ...'."
        )

    def get_iterative_relation_prompt(self, env: PromptEnv, relation_name: str, running_summary: str, relation_summaries_chunk: str, entity_name: Optional[str] = None) -> str:
        """
        Returns the formatted prompt for iterative relation summarization based on relation_name.
        Dispatches to the specific template needed for the relationship type.
        """
        if relation_name == "function_has_callers":
            template = self.get_iterative_caller_prompt_template()
        elif relation_name == "function_has_callees":
            template = self.get_iterative_callee_prompt_template()
        elif relation_name == "class_has_parents": # For class inheritance
            template = self.get_iterative_class_inheritance_prompt_template()
        elif relation_name == "class_has_methods": # For class methods
            template = self.get_iterative_class_method_prompt_template()
        elif relation_name == "namespace_children" or \
             relation_name == "project_children" or \
             relation_name == "folder_children" or \
             relation_name == "file_children": 
            if entity_name is None:
                raise ValueError(f"entity_name must be provided for parent-children relation: {relation_name}.")
            template = self.get_iterative_parent_children_prompt(relation_name, entity_name)
        else:
            raise ValueError(f"Unknown relation_name for iterative prompt: {relation_name}")
        
        prompt = template.format(running_summary=running_summary, relation_summaries_chunk=relation_summaries_chunk)
        return self._apply_env_header(env, prompt)
