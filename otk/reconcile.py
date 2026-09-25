"""Make a repository's tags on the high side match the desired state. Pure planning, no I/O."""


def plan(current, desired, max_delete_pct=50):
    """current/desired: {tag: digest}. Returns (push, delete, refused).

    push: tags to create or retarget. delete: tags to remove. refused: True when the delete share
    exceeds max_delete_pct, because a truncated state and a real mass deletion look the same."""
    push = sorted(t for t, d in desired.items() if current.get(t) != d)
    delete = sorted(t for t in current if t not in desired)
    refused = bool(current) and bool(delete) and len(delete) * 100 > max_delete_pct * len(current)
    return push, ([] if refused else delete), refused
