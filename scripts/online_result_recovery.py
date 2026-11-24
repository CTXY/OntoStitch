"""Apply explicitly accepted guided recoveries without altering baselines or inputs."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path


def fingerprint(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def input_fingerprint(row):
    return fingerprint({k: row.get(k) for k in ("database_id", "source_id", "question", "evidence", "sql")})


def overlay_recoveries(rows, manifest):
    """Validate every reference before returning an immutable replacement ledger."""
    accepted = manifest.get("cases", {})
    updated, seen = [], set()
    for source in rows:
        key = source["database_id"] + "/" + str(source["source_id"])
        entry = accepted.get(key)
        if entry is None:
            updated.append(source)
            continue
        seen.add(key)
        if input_fingerprint(source) != entry["input_fingerprint"]:
            raise ValueError(f"{key}: original case inputs changed")
        if fingerprint(source.get("plain")) != entry["baseline_fingerprint"]:
            raise ValueError(f"{key}: preserved baseline changed")
        path = Path(entry["guided_path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["guided_sha256"]:
            raise ValueError(f"{key}: recovered prediction hash changed")
        prediction = json.loads(path.read_text())
        score = prediction.get("evaluation", {}).get("exec")
        permitted_scores = (0, 1) if manifest.get("include_evaluated_mismatches") else (1,)
        if (
            str(prediction.get("source_id")) != str(source["source_id"])
            or prediction.get("database_id") != source["database_id"]
            or prediction.get("gold_sql") != source["sql"]
            or prediction.get("question") != source["question"]
            or prediction.get("error")
            or score not in permitted_scores
        ):
            raise ValueError(f"{key}: recovery must be an authorized evaluated prediction for the unchanged case")
        prior = source.get("guided")
        if prior and prior.get("evaluation", {}).get("exec") == 1 and prior != prediction:
            raise ValueError(f"{key}: cannot replace a previously passing result")
        row = deepcopy(source)
        update_id = key + ":" + entry["guided_sha256"]
        history = row.setdefault("updates", [])
        if not any(item.get("update_id") == update_id for item in history):
            history.append({
                "update_id": update_id, "stage": "checkpoint_recovery", "arm": "guided",
                "old": {"status": source.get("status"), "case_error": source.get("case_error"),
                        "result_source": source.get("result_sources", {}).get("guided")},
                "new": {"path": str(path), "sha256": entry["guided_sha256"]},
                "accepted_at": manifest["accepted_at"],
            })
        row.update(guided=prediction, case_error=None, completed=True, status="recovered_completed",
                   guided_recovery_folder=str(path.parent), outcome="correct" if score == 1 else "ea_mismatch")
        row.setdefault("result_sources", {})["guided"] = {"path": str(path), "sha256": entry["guided_sha256"]}
        updated.append(row)
    if seen != set(accepted):
        raise ValueError("Accepted recovery refers to cases outside this test set")
    return updated
