"""General utilities for formatting, notebook displays, and CLI configurations.

Provides message rendering (rich console panels), type converters (str2bool),
prompt display layouts, and helper functions to extract SSL verification parameters.
"""
import argparse
import json
import os
import sys

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()


def format_message_content(message):
    """Convert message content to displayable string."""
    parts = []
    tool_calls_processed = False

    # Handle main content
    if isinstance(message.content, str):
        parts.append(message.content)
    elif isinstance(message.content, list):
        # Handle complex content like tool calls (Anthropic format)
        for item in message.content:
            if item.get("type") == "text":
                parts.append(item["text"])
            elif item.get("type") == "tool_use":
                parts.append(f"\n🔧 Tool Call: {item['name']}")
                parts.append(f"   Args: {json.dumps(item['input'], indent=2)}")
                parts.append(f"   ID: {item.get('id', 'N/A')}")
                tool_calls_processed = True
    else:
        parts.append(str(message.content))

    # Handle tool calls attached to the message (OpenAI format) - only if not already processed
    if (
            not tool_calls_processed
            and hasattr(message, "tool_calls")
            and message.tool_calls
    ):
        for tool_call in message.tool_calls:
            parts.append(f"\n🔧 Tool Call: {tool_call['name']}")
            parts.append(f"   Args: {json.dumps(tool_call['args'], indent=2)}")
            parts.append(f"   ID: {tool_call['id']}")

    return "\n".join(parts)


def format_messages(messages):
    """Format and display a list of messages with Rich formatting."""
    for m in messages:
        msg_type = m.__class__.__name__.replace("Message", "")
        content = format_message_content(m)

        if msg_type == "Human":
            console.print(Panel(content, title="🧑 Human", border_style="blue"))
        elif msg_type == "Ai":
            console.print(Panel(content, title="🤖 Assistant", border_style="green"))
        elif msg_type == "Tool":
            # Limit tool output to 10 lines
            lines = content.split('\n')
            if len(lines) > 10:
                content = '\n'.join(lines[:10]) + '\n...'
            console.print(Panel(content, title="🔧 Tool Output", border_style="yellow"))
        else:
            console.print(Panel(content, title=f"📝 {msg_type}", border_style="white"))


def format_message(messages):
    """Alias for format_messages for backward compatibility."""
    return format_messages(messages)


def show_prompt(prompt_text: str, title: str = "Prompt", border_style: str = "blue"):
    """Display a prompt with rich formatting and XML tag highlighting.

    Args:
        prompt_text: The prompt string to display
        title: Title for the panel (default: "Prompt")
        border_style: Border color style (default: "blue")
    """
    # Create a formatted display of the prompt
    formatted_text = Text(prompt_text)
    formatted_text.highlight_regex(r"<[^>]+>", style="bold blue")  # Highlight XML tags
    formatted_text.highlight_regex(
        r"##[^#\n]+", style="bold magenta"
    )  # Highlight headers
    formatted_text.highlight_regex(
        r"###[^#\n]+", style="bold cyan"
    )  # Highlight sub-headers

    # Display in a panel for better presentation
    console.print(
        Panel(
            formatted_text,
            title=f"[bold green]{title}[/bold green]",
            border_style=border_style,
            padding=(1, 2),
        )
    )


def str2bool(v, defaultValue=None):
    """Convert a string representation of a boolean to ``bool``.

    Accepts ``"yes"``, ``"true"``, ``"t"``, ``"y"``, ``"1"`` (case-
    insensitive) as ``True`` and ``"no"``, ``"false"``, ``"f"``, ``"n"``,
    ``"0"`` as ``False``.  Raw ``bool`` values pass through unchanged.

    Args:
        v: The value to convert. May be ``str``, ``bool``, or ``None``.
        defaultValue: Fallback value returned when ``v`` is ``None`` or
            does not match any known boolean representation.  If
            ``defaultValue`` is ``None`` and ``v`` is unrecognized, an
            ``argparse.ArgumentTypeError`` is raised.

    Returns:
        The boolean value.

    Raises:
        argparse.ArgumentTypeError: When ``v`` is unrecognized and
            ``defaultValue`` is ``None``.
    """
    if v is None:
        return defaultValue
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    if v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    if defaultValue is None:
        raise argparse.ArgumentTypeError('Boolean value expected.')
    return defaultValue


def _get_verify_ssl():
    """Resolve the SSL verification flag from CLI args or environment.

    Checks ``--verify_ssl`` / ``--verify_ssl=<val>`` on the command line,
    then falls back to the ``VERIFY_SSL`` environment variable.

    Returns:
        ``True``, ``False``, or a string path to a CA bundle file.
    """
    for i, arg in enumerate(sys.argv):
        if arg.startswith('--verify_ssl='):
            val = arg.split('=', 1)[1]
            return str2bool(val, True)
        elif arg == '--verify_ssl':
            if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith('-'):
                val = sys.argv[i + 1]
                return str2bool(val, True)
            return True
    return str2bool(os.getenv("VERIFY_SSL", "True"), True)


def _get_ssl_ca_file():
    """Resolve an SSL CA bundle file path from CLI args or environment.

    Checks ``--ssl-ca-file`` / ``--ssl-ca-file=<path>`` on the command
    line, then falls back to ``SSL_CAINFO``, ``SSL_CERT_FILE``,
    ``REQUESTS_CA_BUNDLE``, and ``CURL_CA_BUNDLE`` environment variables.

    Returns:
        A file path string if found, or ``None``.
    """
    for i, arg in enumerate(sys.argv):
        if arg.startswith('--ssl-ca-file='):
            return arg.split('=', 1)[1]
        if arg == '--ssl-ca-file' and i + 1 < len(sys.argv):
            return sys.argv[i + 1]

    for env_var in ("SSL_CAINFO", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        value = os.getenv(env_var)
        if value:
            return value

    return None


def get_ssl_verify_config():
    """Return the SSL verification configuration for HTTP clients.

    Resolves ``--verify_ssl`` / ``--ssl-ca-file`` from CLI flags and
    environment variables into a value suitable for ``httpx.Client(verify=...)``
    or ``requests.Session.verify``.

    Returns:
        - ``True`` — use default system CA bundle.
        - ``False`` — disable certificate verification entirely.
        - A file path string — use a custom CA bundle.
    """
    verify_ssl = _get_verify_ssl()
    if not verify_ssl:
        return False

    ssl_ca_file = _get_ssl_ca_file()
    if ssl_ca_file:
        return ssl_ca_file

    return True
