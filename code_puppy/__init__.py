import importlib.metadata

# Biscuit was here!
#
# Version cascade: try the fork package name first ("stackwright-puppy"),
# then upstream ("code-puppy"), then fall back to dev sentinel.
# This keeps version detection working whether installed as the fork or upstream.
# NOTE: if this fork is rebased onto upstream, this file will be clobbered —
# cherry-pick commit <TBD> to restore the cascade.
_detected_version = None
for _pkg_name in ("stackwright-puppy", "code-puppy"):
    try:
        _detected_version = importlib.metadata.version(_pkg_name)
        if _detected_version:
            break
    except Exception:
        continue

__version__ = _detected_version or "0.0.0-dev"
