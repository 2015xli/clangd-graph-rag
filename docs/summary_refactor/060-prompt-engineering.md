# 060-prompt-engineering.md

**Goal**: Update the `PromptManager` to handle the new structural contexts.

#### **1. Interface Summaries**
*   **Prompt**: *"Provide a summary of the interface defined by name: '[Name]', signature: '[Signature]', and return type: '[ReturnType]'. The function has no implementation body code."*

#### **2. Class Manifests (Blueprint + Members)**
*   **Prompt**:
    *   *Context*: Blueprint Summary, Header Code, Specialization Args, Member Inventory.
    *   *Goal*: Summarize the specific behavior and role of the class.

#### **3. SCC Group Context**
*   **Prompt (Step A)**: *"Analyze the collective logic and termination condition for these recursive classes: [List of bodies]."*
*   **Prompt (Step B)**: *"Based on the collective summary '[Group Summary]', explain the specific role of the class '[Name]'."*

#### **4. Manifest Context (File/Folder)**
*   **Prompt**: *"The [File/Folder] named '[Name]' includes these components: [List of manifest entries]. Summarize its overall role in the project."*
