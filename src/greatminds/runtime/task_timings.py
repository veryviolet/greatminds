"""Observed wall-clock intervals; missing or regressing clocks stay unknown."""

import math


def interval(start, end):
    if type(start) not in (int, float) or type(end) not in (int, float):
        return None
    try:
        duration = end - start
        if not math.isfinite(start) or not math.isfinite(end) or not math.isfinite(duration) or duration < 0:
            return None
        return duration
    except OverflowError:
        return None


def summary(run, receipt):
    def read(document, group, key):
        value = document.get(group) if isinstance(document, dict) else None
        return interval(0, value.get(key)) if isinstance(value, dict) else None
    return {
        'observed_queue_wait_seconds': read(run, 'queue_observation', 'wait_seconds'),
        'claim_to_accepted_transition_seconds': read(run, 'domain_progress', 'claim_to_transition_seconds'),
        'preparation_validation_seconds': read(receipt, 'validation', 'seconds'),
        'preparation_validation_attempts': read(receipt, 'validation', 'attempts'),
        'result_received_to_resolution_seconds': read(receipt, 'timings', 'received_to_resolution_seconds'),
        'result_application_elapsed_seconds': read(receipt, 'timings', 'application_elapsed_seconds'),
    }
