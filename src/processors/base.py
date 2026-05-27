"""Abstract base class for reaction event processors."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseProcessor(ABC):
    """Interface that all reaction processors must implement.

    To add a new action (e.g. creating a calendar event, sending a DM, etc.),
    subclass ``BaseProcessor``, implement ``process``, and register the class
    in the ``PROCESSOR_REGISTRY`` inside ``src/processors/__init__.py``.
    Then reference it by name in ``config.yaml`` under ``emoji_mappings[].processor``.
    """

    @abstractmethod
    async def process(self, event: dict, mapping: dict) -> bool:
        """Handle a ``reaction_added`` Slack event.

        Args:
            event:    The raw Slack event payload.
            mapping:  The matching entry from ``emoji_mappings`` in config.yaml.

        Returns:
            ``True`` if the action completed successfully, ``False`` otherwise.
        """
        ...
