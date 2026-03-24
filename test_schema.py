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
    
class Output1(BaseModel):
    blocks: List[Union[BlockA, BlockB]]

class Output2(BaseModel):
    blocks: List[Annotated[Union[BlockA, BlockB], Field(discriminator="block_type")]]

for i, schema in enumerate([Output1, Output2]):
    try:
        # Just try to parse the schema through the SDK
        config = types.GenerateContentConfig(response_schema=schema, response_mime_type="application/json")
        print(f"Schema {i+1} works!")
    except Exception as e:
        print(f"Schema {i+1} FAILED: {e}")
