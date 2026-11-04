#!/usr/bin/env python3
"""Reuse the tested SEND acceptance engine; default to functional regression."""
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent/'send_pool'))
import run_candidate as engine

BASE_COMMIT = 'bdc1e36e9ee75d97dee2d3e93b9e83b6bd10b49f'
BASE_MODULE = '560fd644b66d60d25e5c86bc55be6d44bae5495804c3f48fa8b3ef63744b375a'
BASE_RESULT = '8c5a886923d5db6ef2e50d6685a6e2a3df76974b1a3893a5c1bd372ccb2241e9'

def checkpoint():
    path = engine.OLD_CASE/'send_pool_accept_8vruomlc/results.json'
    if hashlib.sha256(path.read_bytes()).hexdigest() != BASE_RESULT:
        raise ValueError('accepted pool checkpoint changed')
    return {'pool': {'path':str(path), 'sha256':BASE_RESULT, 'rerun':False}}

def configure():
    original_plan = engine.plan
    def plan():
        value = original_plan()
        value.update(product_change='deduplicate control EP construction and wire codecs; preserve protocol and lifetime',
                     prior_product=BASE_COMMIT, performance_requested_by_default=False)
        return value
    engine.ROOT = ROOT
    engine.BASE_COMMIT = BASE_COMMIT
    engine.BASE_MODULE = BASE_MODULE
    engine.OLD_CASE = engine.EXPERIMENT/'r1_shared_bdc1e36e9_gCNkQI'
    engine.RUN_PREFIX = 'send_cleanup_accept_'
    engine.SUCCESS_STATUS = 'SELECTED_SEND_CLEANUP_FUNCTIONAL_REGRESSION_PASS'
    engine.load_prior = checkpoint
    engine.plan = plan

if __name__ == '__main__':
    configure()
    engine.main()
