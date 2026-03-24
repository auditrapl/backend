import asyncio
import os
from pydantic import BaseModel, ConfigDict, Field
from google import genai
from google.genai import types
from typing import Literal, Union, List, Annotated

# Setup
class BlockA(BaseModel):
    block_type: Literal["a"]
    text: str

class BlockB(BaseModel):
    block_type: Literal["b"]
    value: int

class Output2(BaseModel):
    blocks: List[Annotated[Union[BlockA, BlockB], Field(discriminator="block_type")]]

try:
    # Try actual conversion
    print(types.ContentDict)
    # the new SDK converts schemas using an internal helper
    from google.genai import _pydantic_helper
    res = _pydantic_helper.get_schema(Output2)
    print("SUCCESS: ", res)
except Exception as e:
    import traceback
    traceback.print_exc()
