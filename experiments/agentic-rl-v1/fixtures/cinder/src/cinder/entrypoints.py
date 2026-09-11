# Deliberate call-site distractors used by navigation tasks.
from .kestrel_service import kestrel_pipeline

def run_kestrel_job(payload: str) -> str:
    return kestrel_pipeline(payload)

from .linden_service import linden_pipeline

def run_linden_job(payload: str) -> str:
    return linden_pipeline(payload)

from .maple_service import maple_pipeline

def run_maple_job(payload: str) -> str:
    return maple_pipeline(payload)

from .nova_service import nova_pipeline

def run_nova_job(payload: str) -> str:
    return nova_pipeline(payload)

from .river_service import river_pipeline

def run_river_job(payload: str) -> str:
    return river_pipeline(payload)

from .velvet_service import velvet_pipeline

def run_velvet_job(payload: str) -> str:
    return velvet_pipeline(payload)

from .willow_service import willow_pipeline

def run_willow_job(payload: str) -> str:
    return willow_pipeline(payload)

