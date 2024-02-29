import ast
import logging
import uuid
from typing import List, Set

import black
import isort
from prisma.models import (
    APIRouteSpec,
    CompiledRoute,
    CompletedApp,
    Function,
    ObjectField,
    ObjectType,
    Package,
    Specification,
)
from prisma.types import CompiledRouteCreateInput, CompletedAppCreateInput
from pydantic import BaseModel

from codex.api_model import Identifiers
from codex.deploy.model import Application

logger = logging.getLogger(__name__)


class CompiledFunction(BaseModel):
    packages: List[Package]
    imports: List[str]
    code: str
    pydantic_models: List[str] = []


async def compile_route(
    ids: Identifiers, route_root_func: Function, api_route: APIRouteSpec
) -> CompiledRoute:
    """
    Compiles a route by generating a CompiledRoute object.

    Args:
        ids (Identifiers): The identifiers used in the route.
        route_root_func (Function): The root function of the route.
        api_route (APIRouteSpec): The specification of the API route.

    Returns:
        CompiledRoute: The compiled route object.

    """
    compiled_function = await recursive_compile_route(route_root_func)

    unique_packages = list(set([package.id for package in compiled_function.packages]))
    compiled_function.imports.append("from pydantic import BaseModel")
    code = "\n".join(compiled_function.imports)
    code += "\n\n"
    code += "\n\n".join(compiled_function.pydantic_models)
    code += "\n\n"
    code += compiled_function.code
    data = CompiledRouteCreateInput(
        description=api_route.description,
        Packages={"connect": [{"id": package_id} for package_id in unique_packages]},
        fileName=api_route.functionName + "_service.py",
        mainFunctionName=route_root_func.functionName,
        compiledCode=code,
        RootFunction={"connect": {"id": route_root_func.id}},
        ApiRouteSpec={"connect": {"id": api_route.id}},
    )
    compiled_route = await CompiledRoute.prisma().create(data)
    return compiled_route


async def recursive_compile_route(
    in_function: Function, object_type_ids: Set[str] = set()
) -> CompiledFunction:
    """
    Recursively compiles a function and its child functions
    into a single CompiledFunction object.

    Args:
        ids (Identifiers): The identifiers for the function.
        function (Function): The function to compile.

    Returns:
        CompiledFunction: The compiled function.

    Raises:
        ValueError: If the function code is missing.
    """
    # Can't see how to do recursive lookup with prisma, so I'm checking the next
    # layer down each time. This is a bit of a hack, can be improved later.
    function = await Function.prisma().find_unique_or_raise(
        where={"id": in_function.id},
        include={
            "ParentFunction": True,
            "ChildFunction": {"include": {"ApiRouteSpec": True}},
        },
    )
    logger.info(f"Compiling function: {function.id}")

    pydantic_models = []
    new_object_types = set()
    if function.FunctionArgs is not None:
        for arg in function.FunctionArgs:
            pydantic_models.append(process_object_field(arg, new_object_types))

    if function.FunctionReturn is not None:
        pydantic_models.append(
            process_object_field(function.FunctionReturn, new_object_types)
        )

    if function.ChildFunction is None:
        packages = []
        if function.Packages:
            packages = function.Packages
        if function.functionCode is None:
            raise ValueError(f"Leaf Function code is required! {function.id}")
        code = "\n".join(function.importStatements)
        code += "\n\n"
        code += function.functionCode

        try:
            tree = ast.parse(code)
        except Exception as e:
            raise ValueError(f"Syntax error in function code: {e}, {code}")

        return CompiledFunction(
            packages=packages,
            imports=function.importStatements,
            code=function.functionCode,
            pydantic_models=pydantic_models,
        )
    else:
        packages = []
        imports = []
        pydantic_models = []
        code = ""
        for child_function in function.ChildFunction:
            compiled_function = await recursive_compile_route(child_function)
            packages.extend(compiled_function.packages)
            imports.extend(compiled_function.imports)
            pydantic_models.extend(compiled_function.pydantic_models)
            code += "\n\n"
            code += compiled_function.code

        if function.Packages:
            packages.extend(function.Packages)
        imports.extend(function.importStatements)

        if function.functionCode is None:
            raise ValueError(f"Function code is required! {function.id}")

        code += "\n\n"
        code += function.functionCode
        check_code = "\n".join(imports)
        check_code += "\n\n"
        check_code += code

        try:
            tree = ast.parse(check_code)
        except Exception as e:
            raise ValueError(f"Syntax error in function code: {e}, {code}")

        return CompiledFunction(
            packages=packages,
            imports=imports,
            code=code,
            pydantic_models=pydantic_models,
        )


async def create_app(
    ids: Identifiers, spec: Specification, compiled_routes: List[CompiledRoute]
) -> CompletedApp:
    """
    Create an app using the given identifiers, specification, and compiled routes.

    Args:
        ids (Identifiers): The identifiers for the app.
        spec (Specification): The specification for the app.
        compiled_routes (List[CompiledRoute]): The compiled routes for the app.

    Returns:
        CompletedApp: The completed app object.
    """
    if spec.ApiRouteSpecs is None:
        raise ValueError("Specification must have at least one API route.")

    data = CompletedAppCreateInput(
        name=spec.name,
        description=spec.context,
        User={"connect": {"id": ids.user_id}},
        CompiledRoutes={"connect": [{"id": route.id} for route in compiled_routes]},
        Specification={"connect": {"id": spec.id}},
        Application={"connect": {"id": ids.app_id}},
    )
    app = await CompletedApp.prisma().create(data)
    return app


def process_object_type(obj: ObjectType, object_type_ids: Set[str] = set()) -> str:
    """
    Generate a Pydantic object based on the given ObjectType.

    Args:
        obj (ObjectType): The ObjectType to generate the Pydantic object for.

    Returns:
        str: The generated Pydantic object as a string.
    """
    if obj.Fields is None:
        raise ValueError(f"ObjectType {obj.name} has no fields.")

    template: str = ""
    sub_types: List[str] = []
    field_strings: List[str] = []
    for field in obj.Fields:
        if field.typeId is not None:
            sub_types.append(process_object_field(field, object_type_ids))

        field_strings.append(
            f"{' '*4}{field.name}: {field.typeName}  # {field.description}"
        )

    fields = "\n".join(
        [
            f"{' '*4}{field.name}: {field.typeName}  # {field.description}"
            for field in obj.Fields
        ]
    )

    fields: str = "\n".join(field_strings)
    # Returned as a string to preserve class declaration order
    template += "\n\n".join(sub_types)
    template += f"""

class {obj.name}(BaseModel):
    \"\"\"
    {obj.description}
    \"\"\"
{fields}
    """
    return template


def process_object_field(field: ObjectField, object_type_ids: Set[str]) -> str:
    """
    Process an object field and return the Pydantic classes
    generated from the field's type.

    Args:
        field (ObjectField): The object field to process.
        object_type_ids (Set[str]): A set of object type IDs that
                                    have already been processed.

    Returns:
        str: The Pydantic classes generated from the field's type.

    Raises:
        AssertionError: If the field type is None.
    """
    if field.typeId is None or field.typeId in object_type_ids:
        # If the field is a primitive type or we have already processed this object,
        # we don't need to do anything
        return ""

    assert field.Type is not None, "Field type is None"

    object_type_ids.add(field.typeId)

    pydantic_classes = process_object_type(field.Type, object_type_ids)

    return pydantic_classes


def create_server_code(completed_app: CompletedApp) -> Application:
    """
    Args:
        application (Application): _description_

    Returns:
        Application: _description_
    """
    name = completed_app.name
    desc = completed_app.description

    server_code_imports = [
        "from fastapi import FastAPI",
        "from fastapi.responses import JSONResponse",
        "import logging",
        "import io",
        "from typing import *",
    ]
    server_code_header = f"""logger = logging.getLogger(__name__)

app = FastAPI(title="{name}", description='''{desc}''')"""

    service_routes_code = []
    if completed_app.CompiledRoutes is None:
        raise ValueError("Application must have at least one compiled route.")

    packages = []
    main_function_names = set()
    for i, compiled_route in enumerate(completed_app.CompiledRoutes):
        if compiled_route.ApiRouteSpec is None:
            raise ValueError(f"Compiled route {compiled_route.id} has no APIRouteSpec")

        if compiled_route.Packages:
            packages.extend(compiled_route.Packages)
        request = compiled_route.ApiRouteSpec.RequestObject
        response = compiled_route.ApiRouteSpec.ResponseObject

        assert request is not None, f"RequestObject is required for {compiled_route.id}"
        assert (
            response is not None
        ), f"ResponseObject is required for {compiled_route.id}"

        route_path = compiled_route.ApiRouteSpec.path
        logger.info(f"Creating route for {route_path}")
        # import the main function from the service file
        compiled_route_module = compiled_route.fileName.replace(".py", "")
        service_import = f"from {compiled_route_module} import *"
        server_code_imports.append(service_import)

        # Write the api endpoint
        # TODO: pass the request method from the APIRouteSpec
        response_type = "return JSONResponse(content=response)"
        # horrible if if if for type checking
        if response.Fields:
            params = response.Fields
            if (len(params) > 0) and (params[0].typeName == "bytes"):
                response_type = """
    # Convert the bytes to a BytesIO object for streaming
    file_stream = io.BytesIO(response)

    # Set the correct content-type for zip files
    headers = {
        "Content-Disposition": f"attachment; filename="new_file.zip""
    }

    # Return the streaming response
    return StreamingResponse(
        content=file_stream, media_type="application/zip", headers=headers
    )
"""
        assert request.Fields is not None, f"RequestObject {request.id} has no Fields"

        request_param_str = ", ".join(
            [f"{param.name}: {param.typeName}" for param in request.Fields]
        )
        param_names_str = ", ".join([param.name for param in request.Fields])

        # method is a string here even though it should be an enum in the model
        method_name = compiled_route.ApiRouteSpec.method.lower()  # type: ignore
        api_route_name = f"{method_name}_{compiled_route.mainFunctionName}_route"
        if compiled_route.mainFunctionName in main_function_names:
            main_function_names.add(compiled_route.mainFunctionName)

            unique_end = uuid.uuid4().hex[:2]
            api_route_name += f"_{unique_end}"

        route_code = f"""@app.{method_name}("{route_path}")
async def {api_route_name}({request_param_str}):
    try:
        response = {compiled_route.mainFunctionName}({param_names_str})
    except Exception as e:
        logger.exception("Error processing request")
        response = dict()
        response["error"] =  str(e)
        return JSONResponse(content=response)
    {response_type}
"""
        service_routes_code.append(route_code)

    # Compile the server code
    server_code = "\n".join(server_code_imports)
    server_code += "\n\n"
    server_code += server_code_header
    server_code += "\n\n"
    server_code += "\n\n".join(service_routes_code)

    # Update the application with the server code
    sorted_content = isort.code(server_code)
    formatted_code = black.format_str(sorted_content, mode=black.FileMode())
    return Application(
        name=name,
        description=desc,
        server_code=formatted_code,
        completed_app=completed_app,
        packages=packages,
    )
