#!/usr/bin/env python3
"""
Tree-sitter C parser to DOT graph converter
Processes C source files and generates DOT format for visualization
"""

import tree_sitter_c as tsc
from tree_sitter import Language, Parser
import argparse
import sys, logging, re
import os
from pathlib import Path

class ASTToDot:
    def __init__(self):
        self.node_counter = 0
        self.dot_content = []
        self.logger = logging.getLogger(__name__)
        
    def get_node_id(self):
        """Generate unique node ID"""
        self.node_counter += 1
        return f"node_{self.node_counter}"
    
    def escape_label(self, text):
        """Escape special characters for DOT labels"""
        # First, escape backslashes
        text = text.replace('\\', '\\\\')
        # Then escape quotes
        text = text.replace('"', '\\"')
        # Handle newlines and other special characters
        text = text.replace('\n', '\\n').replace('\r', '\\r')
        # Escape angle brackets which can cause issues
        text = text.replace('<', '\\<').replace('>', '\\>')
        # Escape curly braces
        text = text.replace('{', '\\{').replace('}', '\\}')
        # Escape vertical bars
        text = text.replace('|', '\\|')
        return text
    
    def node_to_dot(self, node, parent_id=None):
        """Convert tree-sitter node to DOT format"""
        current_id = self.get_node_id()
        node_text = node.text.decode('utf-8') if node.text else ''
        self.logger.debug(f"Processing Node ID: {current_id}, Type: '{node.type}', Text: '{node_text}', Children: {node.child_count}")
        
        # Create node label with type and text (if it's a leaf)
        if node.child_count == 0 and node.text:
            # Leaf node - show both type and text content
            text_content = node.text.decode('utf-8')
            if len(text_content) > 35:
                text_content = text_content[:15] + " ... " + text_content[-15:]
            
            # Handle empty node types (like quote characters)
            node_type = node.type
            if not node_type:
                self.logger.debug(f"Empty string node type detected for node {current_id}, using 'quote'")
                node_type = "quote"
            elif node_type.isspace() or node_type == '\n':
                self.logger.debug(f"Null/whitespace node type detected for node {current_id}, using 'token'")
                node_type = "token"
            
            escaped_type = self.escape_label(node_type)
            escaped_text = self.escape_label(text_content)
            label = f'{escaped_type}\\n{escaped_text}'
            color = "lightblue"
            
            self.logger.debug(f"Leaf node {current_id}: type='{node_type}'")
        else:
            # Internal node - show only type
            node_type = node.type if node.type else "unknown"
            label = self.escape_label(node_type)
            color = "lightgreen"
        
        # Add node to DOT
        dot_line = f'    {current_id} [label="{label}", fillcolor="{color}", style="filled"];'
        self.dot_content.append(dot_line)
        self.logger.debug(f"Generated DOT line: {dot_line}")
        
        # Add edge from parent if exists
        if parent_id:
            self.dot_content.append(f'    {parent_id} -> {current_id};')
        
        # Process children recursively
        for child in node.children:
            self.node_to_dot(child, current_id)
        
        return current_id
    
    def generate_dot(self, root_node, filename="ast"):
        """Generate complete DOT graph"""
        self.node_counter = 0
        self.dot_content = []
        
        # DOT header
        self.dot_content.extend([
            f"digraph {filename}_ast {{",
            "    rankdir=TB;",
            "    node [shape=rectangle, fontname=\"Arial\", fontsize=10];",
            "    edge [fontname=\"Arial\", fontsize=8];",
            ""
        ])
        
        # Process the tree
        self.node_to_dot(root_node)
        
        # DOT footer
        self.dot_content.append("}")
        
        return "\n".join(self.dot_content)

def parse_c_file(file_path):
    """Parse C file and return AST root node"""
    try:
        # Initialize the C language
        C_LANGUAGE = Language(tsc.language())
        parser = Parser(C_LANGUAGE)
        
        # Read the C file
        with open(file_path, 'r', encoding='utf-8') as f:
            source_code = f.read()
        
        # Parse the source code
        tree = parser.parse(bytes(source_code, "utf8"))
        return tree.root_node, source_code
        
    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.")
        return None, None
    except Exception as e:
        print(f"Error parsing file: {e}")
        return None, None

def save_dot_file(dot_content, output_path, logger):
    """Save DOT content to file"""
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(dot_content)
        logger.debug(f"DOT file saved successfully: {output_path}")
        print(f"DOT file saved: {output_path}")
        return True
    except Exception as e:
        logger.error(f"Error saving DOT file: {e}")
        print(f"Error saving DOT file: {e}")
        return False

def generate_image(dot_file, output_format="png"):
    """Generate image from DOT file using graphviz"""
    try:
        base_name = os.path.splitext(dot_file)[0]
        output_file = f"{base_name}.{output_format}"
        
        # Use graphviz to generate image
        os.system(f"dot -T{output_format} {dot_file} -o {output_file}")
        print(f"Graph image generated: {output_file}")
        return True
    except Exception as e:
        print(f"Error generating image: {e}")
        return False

def write_ast(node, f, indent):
    node_text = node.text.decode('utf-8') if node.text else ''
    node_text = node_text.replace('\n', " ")
    node_text = re.sub(r'\s+', ' ', node_text)
    if len(node_text) > 35:
        node_text = node_text[:15] + " ... " + node_text[-15:]

    f.write(f"{'  ' * indent}Type: {node.type}, Text: {node_text}\n")
    for child in node.children:
        write_ast(child, f, indent + 1)

def save_ast_file(node, output_path, logger, indent=0):
    """Save AST node content to file"""
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            write_ast(node, f, indent)
            
        logger.debug(f"AST node file saved successfully: {output_path}")
        print(f"AST node file saved: {output_path}")
        return True
    except Exception as e:
        logger.error(f"Error saving AST node file: {e}")
        print(f"Error saving AST node file: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Convert C source to DOT AST visualization")
    parser.add_argument("input_file", help="Input C source file")
    parser.add_argument("-o", "--output", help="Output DOT file (default: input_file.dot)")
    parser.add_argument("-f", "--format", default="png", 
                       choices=["png", "svg", "pdf", "jpg"],
                       help="Output image format (default: png)")
    parser.add_argument("--no-image", action="store_true", 
                       help="Don't generate image, only DOT file")
    parser.add_argument("--debug", action="store_true",
                       help="Enable debug logging")
    
    args = parser.parse_args()
    
    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(levelname)s: %(message)s'
    )
    logger = logging.getLogger(__name__)
    
    # Validate input file
    if not os.path.exists(args.input_file):
        print(f"Error: Input file '{args.input_file}' does not exist.")
        return 1
    
    from check_if_c_header import check_if_c_header
    file_path = Path(args.input_file)
    if file_path.suffix.lower() == '.h' and not check_if_c_header(file_path):
        print(f"Error: Input file '{args.input_file}' is not a C header.")
        return 1
    

    # Parse the C file
    print(f"Parsing C file: {args.input_file}")
    root_node, source_code = parse_c_file(args.input_file)
    
    if root_node is None:
        return 1

    print("Generating AST node...")
    #node_file = f"{os.path.splitext(args.input_file)[0]}.ast"
    node_file = f"{args.input_file}.ast"
    if not save_ast_file(root_node, node_file, logger):
        return 1
    
    # Generate DOT content
    print("Generating Dot...")
    ast_converter = ASTToDot()
    base_name = os.path.splitext(os.path.basename(args.input_file))[0]
    dot_content = ast_converter.generate_dot(root_node, base_name)
    
    # Determine output file
    if args.output:
        dot_file = args.output
    else:
        #dot_file = f"{os.path.splitext(args.input_file)[0]}.dot"
        dot_file = f"{args.input_file}.dot"
    
    # Save DOT file
    if not save_dot_file(dot_content, dot_file, logger):
        return 1
    
    # Generate image if requested
    if not args.no_image:
        print(f"Generating {args.format.upper()} image...")
        generate_image(dot_file, args.format)
    
    print("Done!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
