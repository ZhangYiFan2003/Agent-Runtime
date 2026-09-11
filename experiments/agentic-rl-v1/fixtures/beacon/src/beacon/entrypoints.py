# Deliberate call-site distractors used by navigation tasks.
from .falcon_service import falcon_pipeline

def run_falcon_job(payload: str) -> str:
    return falcon_pipeline(payload)

from .grove_service import grove_pipeline

def run_grove_job(payload: str) -> str:
    return grove_pipeline(payload)

from .harbor_service import harbor_pipeline

def run_harbor_job(payload: str) -> str:
    return harbor_pipeline(payload)

from .iris_service import iris_pipeline

def run_iris_job(payload: str) -> str:
    return iris_pipeline(payload)

from .juniper_service import juniper_pipeline

def run_juniper_job(payload: str) -> str:
    return juniper_pipeline(payload)

from .quartz_service import quartz_pipeline

def run_quartz_job(payload: str) -> str:
    return quartz_pipeline(payload)

from .timber_service import timber_pipeline

def run_timber_job(payload: str) -> str:
    return timber_pipeline(payload)

from .umber_service import umber_pipeline

def run_umber_job(payload: str) -> str:
    return umber_pipeline(payload)

