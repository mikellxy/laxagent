"""Collect one batch of human answers through a checkpointed interrupt."""
from typing import Annotated

from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Text = Annotated[str, StringConstraints(pattern=r"\S")]


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: Text
    choice: list[Text] = Field(max_length=3)


class AskUserQuestionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    questions: list[Question] = Field(min_length=1, max_length=5)


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: Text
    answer: Text


class Answers(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answers: list[Answer] = Field(min_length=1, max_length=5)


async def ask_user_question(context, args: AskUserQuestionArgs):
    response = interrupt(args.model_dump())
    return Answers.model_validate(response).model_dump()
