"""Processor registry — maps processor names (used in config.yaml) to classes.

To add a new processor:
  1. Create a module in this package that subclasses ``BaseProcessor``.
  2. Import it here and add an entry to ``PROCESSOR_REGISTRY``.
  3. Reference the key name in ``emoji_mappings[].processor`` in config.yaml.
"""

from .base import BaseProcessor
from .task_processor import TaskProcessor

PROCESSOR_REGISTRY: dict[str, type[BaseProcessor]] = {
    "TaskProcessor": TaskProcessor,
}

__all__ = ["BaseProcessor", "TaskProcessor", "PROCESSOR_REGISTRY"]
