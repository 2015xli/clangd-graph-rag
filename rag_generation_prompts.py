from typing import Optional

class RagGenerationPromptManager:
    def __init__(self):
        pass

    def get_code_analysis_prompt(self, chunk: str, is_first_chunk: bool, is_last_chunk: bool, running_summary: str = "") -> str:
        """
        Returns the prompt for individual code analysis (Pass 1).
        Handles first, middle, and last chunks.
        """
        if is_first_chunk:
            if is_last_chunk:
                return f"Analyze and summarize the purpose of this C/C++ function based on its code:\n\n```cpp\n{chunk}```"
            else:
                return f"Analyze and summarize this C/C++ code, which is the beginning of a larger function/method:\n\n```cpp\n{chunk}```"
        else:
            position_prompt = "This is the end of the function body." if is_last_chunk else "The function body continues after this code."
            return (
                f"The analysis of the first part of a large function/method so far is: \n'{running_summary}'\n\n" 
                f"Here is the next part of the code:\n```cpp\n{chunk}```\n\n" 
                f"{position_prompt}\n\n"
                f"Please provide a new, single-paragraph analysis and summary that combines the previous analysis and summary with this new code."
            )

    def get_contextual_function_prompt(self, code_analysis: str, caller_text: str, callee_text: str) -> str:
        """
        Returns the prompt for contextual function summarization (Pass 2, single pass).
        """
        return (
            f"A C/C++ function or method is described as: '{code_analysis}'.\n"
            f"It is called by functions with these responsibilities: [{caller_text}].\n"
            f"It calls other functions to do the following: [{callee_text}].\n\n"
            f"Based on this context, what is the high-level purpose of this function/method in the overall system? "
            f"Describe it in concise sentences."
        )

    def get_iterative_caller_prompt_template(self) -> str:
        """Returns the template for iterative caller summarization."""
        return (
            "The function being summarized has this purpose: {running_summary}. "
            "It is used by other functions with the following responsibilities: {relation_summaries_chunk}. "
            "Describe the main function's role in relation to its callers."
        )

    def get_iterative_callee_prompt_template(self) -> str:
        """Returns the template for iterative callee summarization."""
        return (
            "So far, a function's role is summarized as: {running_summary}. "
            "It accomplishes this by calling other functions for these purposes: {relation_summaries_chunk}. "
            "Provide a final, comprehensive summary of the function's overall purpose."
        )

    def get_class_summary_prompt(self, class_name: str, parent_text: str, field_text_prompt: str, method_text: str) -> str:
        """
        Returns the prompt for class summarization (Pass 3, single pass).
        """
        return (
            f"A C++ class named '{class_name}' is defined. {parent_text} {field_text_prompt} {method_text}\n\n"
            f"Based on its inheritance, data members, and methods, what is the primary responsibility and role of the '{class_name}' class in the system? "
            f"Describe it in one concise sentence."
        )

    def get_iterative_class_inheritance_prompt_template(self) -> str:
        """Returns the template for iterative class inheritance summarization."""
        return (
            "A class is described as: {running_summary}. "
            "It inherits from parent classes with these responsibilities: {relation_summaries_chunk}. "
            "Refine the summary to include the role of its inheritance."
        )

    def get_iterative_class_method_prompt_template(self) -> str:
        """Returns the template for iterative class method summarization."""
        return (
            "So far, a class's role is summarized as: {running_summary}. "
            "It implements methods to perform these functions: {relation_summaries_chunk}. "
            "Provide a final, comprehensive summary of the class's overall purpose."
        )

    def get_file_summary_prompt(self, file_name: str, summaries_text: str) -> str:
        """
        Returns the prompt for file summarization (Pass 4).
        """
        return (
            f"A file named '{file_name}' contains components with the following responsibilities: [{summaries_text}]. "
            f"What is the overall purpose of this file?"
        )

    def get_folder_summary_prompt(self, folder_name: str, child_summaries_text: str) -> str:
        """
        Returns the prompt for folder summarization (Pass 5).
        """
        return (
            f"A folder named '{folder_name}' contains the following components: [{child_summaries_text}]. "
            f"What is this folder's collective role in the project?"
        )

    def get_project_summary_prompt(self, child_summaries_text: str) -> str:
        """
        Returns the prompt for project summarization (Pass 5).
        """
        return (
            f"A software project contains the following top-level components: [{child_summaries_text}]. "
            f"What is the overall purpose and architecture of this project?"
        )

    def get_namespace_summary_prompt(self, namespace_name: str, child_summaries_text: str) -> str:
        """
        Returns the prompt for namespace summarization (Pass 4).
        """
        return (
            f"A C++ namespace named '{namespace_name}' contains the following components: [{child_summaries_text}]. "
            f"What is this namespace's collective role and purpose?"
        )

    def get_iterative_parent_children_prompt(self, relation_name: str, entity_name: Optional[str] = None) -> str:
        """
        Returns the template for iterative parent children summarization.
        """
        label = relation_name.split('_')[0]
        return (
            f"The {label} '{entity_name}' in this C/C++ software is summarized as: {{running_summary}}\n"
            f"It also contains the following major components: {{relation_summaries_chunk}}\n"
            f"Provide a new, comprehensive summary of the {label} '{entity_name}' on its role and overall purpose."
        )

    def get_iterative_relation_prompt(self, relation_name: str, running_summary: str, relation_summaries_chunk: str, entity_name: Optional[str] = None) -> str:
        """
        Returns the formatted prompt for iterative relation summarization based on relation_name.
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
        
        return template.format(running_summary=running_summary, relation_summaries_chunk=relation_summaries_chunk)
