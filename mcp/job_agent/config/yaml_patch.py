"""Patch YAML value spans, leaving all unrelated bytes and comments alone."""
import yaml
from yaml.nodes import MappingNode, ScalarNode
from yaml.tokens import AliasToken, AnchorToken


def document(text):
    if any(isinstance(t, (AliasToken, AnchorToken)) for t in yaml.scan(text)):
        raise ValueError('Settings edits do not support YAML anchors or aliases')
    root = yaml.compose(text)
    def check(node):
        if isinstance(node, MappingNode):
            keys = set()
            for key, value in node.value:
                if not isinstance(key, ScalarNode) or key.value == '<<' or key.value in keys:
                    raise ValueError('Settings edits require unique scalar YAML keys without merges')
                keys.add(key.value)
                check(value)
        elif isinstance(node, yaml.SequenceNode):
            for value in node.value:
                check(value)
    if root is not None:
        check(root)
        if not isinstance(root, MappingNode):
            raise ValueError('Settings YAML must be a mapping')
    return root


def render(value, node=None):
    style = None
    if isinstance(value, str):
        if '\n' in value:
            style = '"'
        elif isinstance(node, ScalarNode) and node.style in {"'", '"'}:
            style = node.style
    result = yaml.safe_dump(value, allow_unicode=True, default_flow_style=True,
                            default_style=style, width=1_000_000).rstrip('\n')
    if result.endswith('\n...'):
        result = result[:-4]
    return result


def patch_value(text, parts, value):
    newline_style = '\r\n' if '\r\n' in text else '\n'
    root = document(text)
    if root is None:
        nested = value
        for part in reversed(parts):
            nested = {part: nested}
        prefix = '' if not text or text.endswith('\n') else '\n'
        return text + prefix + yaml.safe_dump(nested, allow_unicode=True, sort_keys=False)
    node = root
    for index, part in enumerate(parts):
        if not isinstance(node, MappingNode):
            # Nullable models (e.g. rate: null) become a new inline target block.
            nested = value
            for name in reversed(parts[index:]):
                nested = {name: nested}
            return text[:node.start_mark.index] + render(nested) + text[node.end_mark.index:]
        children = {k.value: v for k, v in node.value}
        if part in children:
            child = children[part]
            if index < len(parts)-1:
                node = child
                continue
            start, end = child.start_mark.index, child.end_mark.index
            # A block sequence/mapping end mark includes following whitespace/comments.
            # Only replace up to its final value; leave trailing comments untouched.
            def content_end(n):
                if isinstance(n, MappingNode) and n.value and not n.flow_style:
                    return content_end(n.value[-1][1])
                if isinstance(n, yaml.SequenceNode) and n.value and not n.flow_style:
                    return content_end(n.value[-1])
                return n.end_mark.index
            end = content_end(child)
            if isinstance(child, ScalarNode) and child.style in {'|', '>'} and isinstance(value, str):
                # Keep the block style and its header comment. PyYAML chooses the
                # chomping indicator needed to represent the exact new string.
                lines = yaml.safe_dump(value, allow_unicode=True, default_style=child.style).splitlines()
                header = text[start:text.find('\n', start) if '\n' in text[start:] else len(text)]
                comment = ' ' + header[header.index('#'):].rstrip('\r') if '#' in header else ''
                key_column = next(k.start_mark.column for k, v in node.value if v is child)
                replacement = lines[0] + comment + newline_style
                replacement += ''.join(' '*key_column + line + newline_style for line in lines[1:])
                return text[:start] + replacement + text[child.end_mark.index:]
            replacement = render(value, child)
            if isinstance(child, (MappingNode, yaml.SequenceNode)) and not child.flow_style:
                # A block collection starts on the next line; an inline value must
                # follow the key's colon instead (else `key:\n  [a, b]` is invalid).
                key = next(k for k, v in node.value if v is child)
                colon = text.index(':', key.end_mark.index)
                return text[:colon+1] + ' ' + replacement + text[end:]
            return text[:start] + replacement + text[end:]
        nested = value
        for name in reversed(parts[index+1:]):
            nested = {name: nested}
        entry = render(part) + ': ' + render(nested)
        if node.flow_style:
            insert = node.end_mark.index - 1
            return text[:insert] + (', ' if node.value else '') + entry + text[insert:]
        # Insert after the final value's line, before the next sibling or trailing comments.
        last = node.value[-1][1]
        end = last.end_mark.index
        if ((isinstance(last, (MappingNode, yaml.SequenceNode)) and not last.flow_style)
                or (isinstance(last, ScalarNode) and last.style in {'|', '>'})):
            insert = end if end == len(text) else end - last.end_mark.column
        else:
            newline = text.find('\n', end)
            insert = len(text) if newline < 0 else newline + 1
        prefix = '' if insert == 0 or text[insert-1] == '\n' else '\n'
        indent = node.start_mark.column
        return text[:insert] + prefix.replace('\n', newline_style) + ' '*indent + entry + newline_style + text[insert:]
    raise ValueError('Empty settings path')
