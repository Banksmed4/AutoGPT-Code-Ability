import logging
from typing import List

import pydantic

logger = logging.getLogger(__name__)


# WIP: Ideas as code :) I dont intend the schema to stay in this file but I want to get the ideas down
# For the soultion architect agent it needs to have a list of api routes for it to write and context of what the app is it is writing
class APIRouteRequirement(pydantic.BaseModel):
    # I'll use these to generate the endpoint
    method: str
    path: str

    # This is context on what this api route should do
    description: str

    # This is the model for the request and response
    request_model: dict
    response_model: dict

    # This is the database schema this api route will use
    # I'm thinking it will be a prisma table schema or maybe a list of table schemas
    # See the schema.prisma file in the codex directory more info
    database_schema: str

    # Maybe someting about authentication heere too?
    # Need to define a common way of thinking about user roles and permissions


class ApplicationRequirements(pydantic.BaseModel):
    # Application name
    name: str
    # Context on what the application is
    context: str

    api_routes: List[APIRouteRequirement]


def define_requirements(task: str) -> ApplicationRequirements:
    """
    Takes a task and defines the requirements for the task

    Relevant chains:

    codex/chains/decompose_task.py

    TODO: Work out the interface for this
    """
    pass


def hardcoded_requirements(task: str) -> ApplicationRequirements:
    """

    This will take the application name and return the manualy
    defined requirements for the application in the correct format
    """
    logger.warning("⚠️ Using hardcoded requirements")
    pass
