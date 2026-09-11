# Deliberate call-site distractors used by navigation tasks.
from .amber_service import amber_pipeline

def run_amber_job(payload: str) -> str:
    return amber_pipeline(payload)

from .birch_service import birch_pipeline

def run_birch_job(payload: str) -> str:
    return birch_pipeline(payload)

from .cedar_service import cedar_pipeline

def run_cedar_job(payload: str) -> str:
    return cedar_pipeline(payload)

from .dune_service import dune_pipeline

def run_dune_job(payload: str) -> str:
    return dune_pipeline(payload)

from .ember_service import ember_pipeline

def run_ember_job(payload: str) -> str:
    return ember_pipeline(payload)

from .onyx_service import onyx_pipeline

def run_onyx_job(payload: str) -> str:
    return onyx_pipeline(payload)

from .pine_service import pine_pipeline

def run_pine_job(payload: str) -> str:
    return pine_pipeline(payload)

from .summit_service import summit_pipeline

def run_summit_job(payload: str) -> str:
    return summit_pipeline(payload)

