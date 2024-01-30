import ast
import logging
import os
import re
import tempfile
import zipfile
from collections import defaultdict
from enum import Enum
from typing import List

from langchain.pydantic_v1 import BaseModel

from codex.chains.decompose_task import ExecutionPath

from .chains.gen_branching_graph import ElseIf, NodeDef, NodeGraph, NodeTypeEnum
from .db_model import FunctionData, Node, RequiredPackage

logger = logging.getLogger(__name__)


class CodeableNodeTypeEnum(Enum):
    START = "start"
    IF = "if"
    ELIF = "elif"
    ELSE = "else"
    ACTION = "action"
    END = "end"


class CodeableNode(BaseModel):
    indent_level: int
    node_type: CodeableNodeTypeEnum
    node: NodeDef | None = None
    elseif: ElseIf | None = None

    class Config:
        use_enum_values = True
        arbitrary_types_allowed = True

    def __str__(self):
        if self.node_type == CodeableNodeTypeEnum.START.value:
            out = f"def {self.node.name}("
            if self.node.output_params:
                for param in self.node.output_params:
                    out += f"{param.name}: {param.param_type}, "
                out = out[:-2]
            out += "):"
            return out
        elif self.node_type == CodeableNodeTypeEnum.IF.value:
            return f"if {self.node.python_if_condition}:"
        elif self.node_type == CodeableNodeTypeEnum.ELIF.value:
            return f"elif {self.elseif.python_condition}:"
        elif self.node_type == CodeableNodeTypeEnum.ELSE.value:
            return "else:"
        elif self.node_type == CodeableNodeTypeEnum.ACTION.value:
            out = ""
            if self.node.output_params:
                for output_param in self.node.output_params:
                    out += f"{output_param.name}, "
                out = out[:-2]
                out += " = "
            out += f"{self.node.name}("
            if self.node.input_params:
                for input_param in self.node.input_params:
                    out += f"{input_param.name}, "
                out = out[:-2]
            out += ")"
            return out
        elif self.node_type == CodeableNodeTypeEnum.END.value:
            out = ""
            if self.node.input_params:
                if len(self.node.input_params) == 1:
                    out += f"return {self.node.input_params[0].name}"
                else:
                    out += "return ("
                    for param in self.node.input_params:
                        out += f"{param.name}, "
                    out = out[:-2]
                    out += ")"
            return out
        else:
            raise ValueError(f"Invalid node type: {self.node_type}")


def generate_requirements_txt(packages: List[RequiredPackage]) -> str:
    resolved_packages = defaultdict(list)

    # Aggregate versions and specifiers for each package
    for package in packages:
        resolved_packages[package.package_name].append(
            (package.version, package.specifier)
        )

    requirements = []
    for package, versions_specifiers in resolved_packages.items():
        # Handle different cases of version and specifier here
        # For simplicity, we just pick the first version and specifier encountered
        # More complex logic might be needed depending on the requirement
        version, specifier = versions_specifiers[0]
        if version and specifier:
            requirement = f"{package}{specifier}{version}"
        elif version:
            requirement = f"{package}=={version}"
        else:
            requirement = package
        requirements.append(requirement)

    return "\n".join(requirements)


def compile_graph(
    graph: NodeGraph, node_implementations: List[Node], ep: ExecutionPath
):
    # Check if the graph is a DAG
    python_file = ""
    requirements = []
    function_name = (
        ep.name.strip().replace(" ", "_").replace("-", "_").replace("/", "").lower()
        + "_request"
    )

    graph_script = convert_graph_to_code(graph, function_name)

    for node in node_implementations:
        if node.code:
            python_file += f"\n\n{node.code}"
        if node.required_packages:
            requirements.extend(node.required_packages)

    python_file += f"\n\n{graph_script}"
    requirements_txt = generate_requirements_txt(requirements)

    return FunctionData(
        function_name=function_name,
        code=python_file,
        requirements_txt=requirements_txt,
        endpoint_name=ep.endpoint_name,
        graph=graph,
    )


def convert_graph_to_code(node_graph: NodeGraph, function_name: str) -> str:
    codeable_nodes = pre_process_nodes(node_graph)
    return graph_to_code(codeable_nodes, function_name)


def pre_process_nodes(node_graph: NodeGraph) -> List[CodeableNode]:
    codeable_nodes = []

    # Preprocess the node graph to resolve if branching
    skip_node = []
    indent_level = 0

    for i, node in enumerate(node_graph.nodes):
        if node.name in skip_node:
            continue
        if node.node_type == NodeTypeEnum.START.value:
            cnode = CodeableNode(
                indent_level=indent_level,
                node_type=CodeableNodeTypeEnum.START,
                node=node,
            )
            codeable_nodes.append(cnode)
            indent_level += 1
        elif node.node_type == NodeTypeEnum.IF.value:
            commonon_descendent = find_common_descendent(node_graph.nodes, node, i)
            codeable_nodes.append(
                CodeableNode(
                    indent_level=indent_level,
                    node_type=CodeableNodeTypeEnum.IF,
                    node=node,
                )
            )
            skip, code = process_if_paths(
                node_graph,
                node,
                node.true_next_node_name,
                commonon_descendent,
                indent_level + 1,
            )
            skip_node.extend(skip)
            codeable_nodes.extend(code)
            if node.elifs:
                for elifs in node.elifs:
                    codeable_nodes.append(
                        CodeableNode(
                            indent_level=indent_level,
                            node_type=CodeableNodeTypeEnum.ELIF,
                            node=None,
                            elseif=elifs,
                        )
                    )
                    skip, code = process_if_paths(
                        node_graph,
                        node,
                        elifs.true_next_node_name,
                        commonon_descendent,
                        indent_level + 1,
                    )
                    skip_node.extend(skip)
                    codeable_nodes.extend(code)

            if node.false_next_node_name != commonon_descendent:
                codeable_nodes.append(
                    CodeableNode(
                        indent_level=indent_level,
                        node_type=CodeableNodeTypeEnum.ELSE,
                        node=node,
                    )
                )
                skip, code = process_if_paths(
                    node_graph,
                    node,
                    node.false_next_node_name,
                    commonon_descendent,
                    indent_level + 1,
                )
                skip_node.extend(skip)
                codeable_nodes.extend(code)
        elif node.node_type == NodeTypeEnum.ACTION.value:
            codeable_nodes.append(
                CodeableNode(
                    indent_level=indent_level,
                    node_type=CodeableNodeTypeEnum.ACTION,
                    node=node,
                )
            )
        elif node.node_type == NodeTypeEnum.END.value:
            codeable_nodes.append(
                CodeableNode(
                    indent_level=indent_level,
                    node_type=CodeableNodeTypeEnum.END,
                    node=node,
                )
            )
            indent_level -= 1

    return codeable_nodes


def graph_to_code(codeable_nodes: List[CodeableNode], function_name: str) -> str:
    code = ""
    indent_str = "    "
    for node in codeable_nodes:
        if node.node_type == CodeableNodeTypeEnum.START.value:
            node_code = str(node)
            node_code = node_code.replace(node.node.name, function_name)
            code += indent_str * node.indent_level + node_code + "\n"
        else:
            code += indent_str * node.indent_level + str(node) + "\n"
    return code


def process_if_paths(
    node_graph: NodeGraph,
    node: NodeDef,
    next_node_name: str,
    commonon_descendent: str,
    indent_level: int,
) -> List[CodeableNode]:
    codeable_nodes = []
    skip_nodes = []
    next_node = next_node_name
    for subnode in node_graph.nodes:
        if subnode.name != commonon_descendent and subnode.name == next_node:
            codeable_nodes.append(
                CodeableNode(
                    indent_level=indent_level, node_type=subnode.node_type, node=subnode
                )
            )
            skip_nodes.append(subnode.name)
            next_node = subnode.next_node_name
    return skip_nodes, codeable_nodes


def find_common_descendent(nodes: List[Node], node: NodeDef, i: int) -> str:
    commonon_descendent = ""
    descendents = []
    for subnode in nodes[i + 1 :]:
        if subnode.name in descendents:
            commonon_descendent = subnode.name
            return commonon_descendent
        descendents.append(subnode.next_node_name)
    return commonon_descendent


class CompilerError(Exception):
    pass

def analyze_function_signature(code: str, function_name: str):
    # Parse the code into an AST (Abstract Syntax Tree)
    try:
        tree = ast.parse(code)
    except Exception as e:
        logger.error(f"Error parsing code: {e}")
        logger.error(f"Code:\n {code}")
        raise CompilerError("Error parsing code")

    # Find the definition of the function
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            # Extract the parameters
            params = [arg for arg in node.args.args]

            # Check for return annotation
            return_type = None
            if node.returns:
                # Convert the return type node to string
                return_type = ast.dump(node.returns, annotate_fields=False)

            return params, return_type

    return None, None


def create_fastapi_server(functions_data: List[FunctionData]) -> bytes:
    """
    Create a FastAPI server with multiple endpoints using a list of Pydantic objects for function data.
    """
    # Create a temporary directory
    with tempfile.TemporaryDirectory() as temp_dir:
        app_dir = os.path.join(temp_dir, "project")
        os.makedirs(app_dir, exist_ok=True)

        combined_requirements = set(["fastapi\n", "uvicorn\n", "pydantic\n"])
        import_statements = "from fastapi import FastAPI, Body\nfrom pydantic import BaseModel\n\napp = FastAPI()\n"
        endpoint_functions = ""

        # Process each function data Pydantic object
        for idx, function_data in enumerate(functions_data):
            # Analyze the function
            try:
                params, return_type = analyze_function_signature(
                    function_data.code, function_data.function_name
                )
            except CompilerError as e:
                logger.error(f"Node Graph for failed compilation:\n{function_data.graph.json()}")
                raise e
            except Exception as e:
                raise e
            
            if not params:
                logger.error(
                    f"Function {function_data.function_name} has no parameters: Details:\n {function_data.code}"
                )
                raise ValueError(
                    f"Function {function_data.function_name} has no parameters"
                )

            # Sanitize the endpoint name
            sanitized_endpoint_name = re.sub(
                r"\W|^(?=\d)", "_", function_data.endpoint_name
            )

            # Write the code to a uniquely named service file
            service_file_name = f"service_{idx}.py"
            service_file_path = os.path.join(app_dir, service_file_name)
            with open(service_file_path, "w") as service_file:
                service_file.write(function_data.code)

            service_node_graph_file_name = f"service_{idx}_node_graph.json"
            service_node_graph_file_path = os.path.join(
                app_dir, service_node_graph_file_name
            )
            with open(service_node_graph_file_path, "w") as service_node_graph_file:
                service_node_graph_file.write(function_data.graph.json())

            # Import statement for the function
            import_statements += (
                f"from project.service_{idx} import {function_data.function_name}\n"
            )

            # Generate Pydantic models and endpoint
            if len(params) > 1:
                # Create Pydantic model for request
                request_model = f"class RequestModel{idx}(BaseModel):\n"
                request_model += "\n".join([f"    {param.arg}: {ast.unparse(param.annotation)}" for param in params])

                # Add to endpoint functions
                endpoint_functions += f"""
{request_model}

@app.post("/{sanitized_endpoint_name}")
def endpoint_{idx}(request: RequestModel{idx}):
    result = {function_data.function_name}(**request.dict())
    return result
"""
            else:
                # Generate endpoint without Pydantic models
                params_str = ", ".join([f"{param.arg}: {ast.unparse(param.annotation)}" for param in params])
                endpoint_functions += f"""
@app.get("/{sanitized_endpoint_name}")
def endpoint_{idx}({params_str}):
    return {function_data.function_name}({', '.join([param.arg for param in params])})
"""

            # Add requirements
            combined_requirements.update(function_data.requirements_txt.splitlines())

        # Write server.py with imports at the top

        server_file_path = os.path.join(app_dir, "server.py")
        with open(server_file_path, "w") as server_file:
            server_file.write(import_statements + endpoint_functions)

        init_file_path = os.path.join(app_dir, "__init__.py")
        with open(init_file_path, "w") as init_file:
            init_file.write("")
            pass

        # Write combined requirements to requirements.txt
        requirements_file_path = os.path.join(app_dir, "requirements.txt")
        with open(requirements_file_path, "w") as requirements_file:
            requirements_file.write("\n".join(combined_requirements))

        # Create a zip file of the directory
        zip_file_path = os.path.join(app_dir, "server.zip")
        with zipfile.ZipFile(zip_file_path, "w") as zipf:
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if file == "server.zip":
                        continue
                    file_path = os.path.join(root, file)
                    zipf.write(file_path, os.path.relpath(file_path, temp_dir))

        # Read and return the bytes of the zip file
        with open(zip_file_path, "rb") as zipf:
            zip_bytes = zipf.read()

    return zip_bytes