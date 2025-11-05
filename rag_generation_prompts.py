from typing import Optional

class RagGenerationPromptManager:
    def __init__(self):
        pass

    def get_code_summary_prompt(self, chunk: str, is_first_chunk: bool, is_last_chunk: bool, running_summary: str = "") -> str:
        """
        Returns the prompt for individual code summarization (Pass 1).
        Handles first, middle, and last chunks.
        """
        if is_first_chunk:
            if is_last_chunk:
                return f"Summarize the purpose of this C/C++ function based on its code:\n\n```cpp\n{chunk}```"
            else:
                return f"Summarize this C/C++ code, which is the beginning of a larger function/method:\n\n```cpp\n{chunk}```"
        else:
            position_prompt = "This is the end of the function body." if is_last_chunk else "The function body continues after this code."
            return (
                f"The summary of a function/method so far is: \n'{running_summary}'\n\n" 
                f"Here is the next part of the code:\n```cpp\n{chunk}```\n\n" 
                f"{position_prompt}\n\n"
                f"Please provide a new, single-paragraph summary that combines the previous summary with this new code."
            )

    def get_contextual_function_prompt(self, code_summary: str, caller_text: str, callee_text: str) -> str:
        """
        Returns the prompt for contextual function summarization (Pass 2, single pass).
        """
        return (
            f"A C/C++ function or method is described as: '{code_summary}'.\n"
            f"It is called by functions with these responsibilities: [{caller_text}].\n"
            f"It calls other functions to do the following: [{callee_text}].\n\n"
            f"Based on this context, what is the high-level purpose of this function/method in the overall system? "
            f"Describe it in one concise sentence."
        )

    def get_iterative_caller_prompt_template(self) -> str:
        """Returns the template for iterative caller summarization."""
        return (
            "The function being summarized has this purpose: {summary}. "
            "It is used by other functions with the following responsibilities: {relations}. "
            "Describe the main function's role in relation to its callers."
        )

    def get_iterative_callee_prompt_template(self) -> str:
        """Returns the template for iterative callee summarization."""
        return (
            "So far, a function's role is summarized as: {summary}. "
            "It accomplishes this by calling other functions for these purposes: {relations}. "
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
            "A class is described as: {summary}. "
            "It inherits from parent classes with these responsibilities: {relations}. "
            "Refine the summary to include the role of its inheritance."
        )

    def get_iterative_class_method_prompt_template(self) -> str:
        """Returns the template for iterative class method summarization."""
        return (
            "So far, a class's role is summarized as: {summary}. "
            "It implements methods to perform these functions: {relations}. "
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

    def get_iterative_namespace_children_prompt(self, namespace_name: str, summary: str, relations: str) -> str:
        """
        Returns the template for iterative namespace children summarization.
        """
        return (
            f"The namespace '{namespace_name}' is summarized as: {{summary}}. "
            f"It also contains the following components: {{relations}}. "
            f"Provide a new, comprehensive summary of the namespace's overall purpose."
        )

    def get_iterative_relation_prompt(self, relation_name: str, summary: str, relations: str, context_name: Optional[str] = None) -> str:
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
        elif relation_name == "namespace_children": # For namespace children
            if context_name is None:
                raise ValueError("context_name must be provided for namespace_children relation_name.")
            return self.get_iterative_namespace_children_prompt(context_name, summary, relations) # This template is already formatted
        else:
            raise ValueError(f"Unknown relation_name for iterative prompt: {relation_name}")
        return template.format(summary=summary, relations=relations)
