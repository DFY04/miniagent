
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type, Union

registerbill={}
def register(name:str):
    def decrecotr(cls):
        cls.name=name
        registerbill[name]=cls
        return cls
    return  decrecotr


class BaseTool(ABC):
