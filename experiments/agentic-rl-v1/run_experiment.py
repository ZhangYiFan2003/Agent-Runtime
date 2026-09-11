from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import platform
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

import accelerate
import peft
import torch
import transformers
import trl
from datasets import Dataset
from environment import AxiomRepositoryEnvironment
from peft import LoraConfig, PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, TrainerCallback, set_seed
from trl import GRPOConfig, GRPOTrainer

ROOT = Path(__file__).resolve().parent
DATASET_DIR = ROOT / "dataset"
RESULTS_DIR = ROOT / "results"
CHECKPOINTS_DIR = ROOT / "checkpoints"
MODEL_ID = "Qwen/Qwen3-0.6B"
SEED = 20260911
TRAIN_NUM_GENERATIONS = 4
EVAL_NUM_GENERATIONS = 2
TRAIN_TEMPERATURE = 1.0
EVAL_TEMPERATURE = 0.7
MAX_COMPLETION_LENGTH = 384
MAX_TOOL_ITERATIONS = 4
TRAIN_STEPS = 42

SYSTEM_PROMPT = """You are a repository-navigation agent in a deterministic fixture.
The tools are already rooted at the current repository. Use search_repository(query) instead of
guessing from naming patterns. First search for relevant code, then read
the evidence file. Submit exactly one repository-relative file path and exact symbol with
submit_answer. If submission is NOT_VERIFIED, gather better evidence and retry. Once VERIFIED,
stop. Keep the final response short."""


def _json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_rows(split: str) -> list[dict[str, Any]]:
    path = DATASET_DIR / f"{split}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def load_dataset(split: str, *, limit: int | None = None) -> Dataset:
    rows = _load_rows(split)
    if limit is not None:
        rows = rows[:limit]
    trainer_rows = []
    for row in rows:
        trainer_rows.append(
            {
                **row,
                "prompt": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Repository ID: {row['repo']} (this is not a path argument)\n"
                            f"Task: {row['question']}"
                        ),
                    },
                ],
            }
        )
    return Dataset.from_list(trainer_rows)


def _manifest() -> dict[str, Any]:
    return json.loads((DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))


def _runtime_fingerprint() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "torch": torch.__version__,
        "torch_cuda_runtime": torch.version.cuda,
        "cuda_available": torch.cuda.is_available(),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "gpu_total_mib": (
            round(torch.cuda.get_device_properties(0).total_memory / 2**20, 1)
            if torch.cuda.is_available()
            else None
        ),
        "trl": trl.__version__,
        "transformers": transformers.__version__,
        "peft": peft.__version__,
        "accelerate": accelerate.__version__,
    }


def _experiment_config(*, max_steps: int, reward: str) -> dict[str, Any]:
    return {
        "model": MODEL_ID,
        "algorithm": "GRPO",
        "trainer": "TRL",
        "seed": SEED,
        "reward_config": reward,
        "train_num_generations": TRAIN_NUM_GENERATIONS,
        "eval_num_generations": EVAL_NUM_GENERATIONS,
        "train_temperature": TRAIN_TEMPERATURE,
        "eval_temperature": EVAL_TEMPERATURE,
        "top_p": 0.9,
        "max_completion_length": MAX_COMPLETION_LENGTH,
        "max_tool_calling_iterations": MAX_TOOL_ITERATIONS,
        "scale_rewards": "group",
        "loss_type": "dapo",
        "beta": 0.0,
        "learning_rate": 5e-6,
        "per_device_train_batch_size": 1,
        "gradient_accumulation_steps": 4,
        "max_steps": max_steps,
        "precision": "bfloat16",
        "gradient_checkpointing": True,
        "peft": {
            "type": "LoRA",
            "r": 8,
            "alpha": 16,
            "dropout": 0.0,
            "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"],
        },
        "dataset_fingerprint": _manifest()["dataset_fingerprint"],
    }


def _grpo_args(stage: str, *, max_steps: int) -> GRPOConfig:
    generation_temperature = (
        TRAIN_TEMPERATURE if stage.endswith("_train") else EVAL_TEMPERATURE
    )
    return GRPOConfig(
        output_dir=str(CHECKPOINTS_DIR / f"{stage}-trainer-state"),
        seed=SEED,
        data_seed=SEED,
        max_steps=max_steps,
        per_device_train_batch_size=1,
        # Evaluation batches must contain a complete GRPO group.
        per_device_eval_batch_size=EVAL_NUM_GENERATIONS,
        gradient_accumulation_steps=4,
        learning_rate=5e-6,
        num_generations=TRAIN_NUM_GENERATIONS,
        num_generations_eval=EVAL_NUM_GENERATIONS,
        temperature=generation_temperature,
        top_p=0.9,
        max_completion_length=MAX_COMPLETION_LENGTH,
        max_tool_calling_iterations=MAX_TOOL_ITERATIONS,
        scale_rewards="group",
        loss_type="dapo",
        beta=0.0,
        bf16=True,
        fp16=False,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        use_cache=False,
        logging_steps=1,
        save_strategy="no",
        eval_strategy="no",
        report_to="none",
        remove_unused_columns=False,
        shuffle_dataset=False,
        chat_template_kwargs={"enable_thinking": False},
    )


def _lora_config() -> LoraConfig:
    return LoraConfig(
        r=8,
        lora_alpha=16,
        lora_dropout=0.0,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    )


def _load_tokenizer():
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"
    return tokenizer


def _load_base_model():
    model = AutoModelForCausalLM.from_pretrained(MODEL_ID, dtype=torch.bfloat16)
    model.config.use_cache = False
    return model


class GradientAuditCallback(TrainerCallback):
    def __init__(self) -> None:
        self.pre_optimizer_steps = 0
        self.nonfinite_gradient_tensors = 0
        self.gradient_tensors = 0

    def on_pre_optimizer_step(self, args, state, control, model=None, **kwargs):
        del args, state, kwargs
        self.pre_optimizer_steps += 1
        if model is not None:
            for parameter in model.parameters():
                if parameter.grad is None:
                    continue
                self.gradient_tensors += 1
                if not torch.isfinite(parameter.grad).all().item():
                    self.nonfinite_gradient_tensors += 1
        return control


class ArtifactGRPOTrainer(GRPOTrainer):
    def __init__(self, *args, artifact_path: Path, stage: str, **kwargs) -> None:
        self._artifact_path = artifact_path
        self._artifact_stage = stage
        self._last_tool_masks = None
        self._last_structured_completions = None
        if artifact_path.exists():
            raise FileExistsError(f"refusing to overwrite frozen artifact: {artifact_path}")
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(*args, **kwargs)

    def _generate(self, prompts: list):
        generated = super()._generate(prompts)
        self._last_tool_masks = generated[2]
        self._last_structured_completions = generated[3]
        return generated

    def _generate_and_score_completions(self, inputs):
        output = super()._generate_and_score_completions(inputs)
        if not self.environments:
            return output
        prompt_ids = output["prompt_ids"].detach().cpu()
        prompt_masks = output["prompt_mask"].detach().cpu()
        completion_ids = output["completion_ids"].detach().cpu()
        completion_masks = output["completion_mask"].detach().cpu()
        advantages = output["advantages"].detach().cpu()
        records = []
        for index, environment in enumerate(self.environments):
            prompt = prompt_ids[index][prompt_masks[index].bool()].tolist()
            completion = completion_ids[index][completion_masks[index].bool()].tolist()
            completion_mask = completion_masks[index][: len(completion)].int().tolist()
            raw_tool_mask = (
                self._last_tool_masks[index]
                if self._last_tool_masks is not None
                else [1] * len(completion)
            )
            tool_mask = list(raw_tool_mask[: len(completion)])
            if len(tool_mask) < len(completion):
                tool_mask.extend([1] * (len(completion) - len(tool_mask)))
            structured = (
                self._last_structured_completions[index]
                if self._last_structured_completions is not None
                else []
            )
            record = environment._artifact_record(
                prompt_token_ids=prompt,
                completion_token_ids=completion,
                completion_mask=completion_mask,
                tool_mask=tool_mask,
                completion_text=json.dumps(structured, ensure_ascii=False, default=str),
                advantage=float(advantages[index].item()),
                stage=self._artifact_stage,
            )
            record["model_id"] = MODEL_ID
            record["dataset_fingerprint"] = _manifest()["dataset_fingerprint"]
            record["sampling"] = {
                "seed": SEED,
                "temperature": (
                    TRAIN_TEMPERATURE
                    if self._artifact_stage.endswith("_train")
                    else EVAL_TEMPERATURE
                ),
                "top_p": 0.9,
                "max_completion_length": MAX_COMPLETION_LENGTH,
                "num_generations": (
                    TRAIN_NUM_GENERATIONS
                    if self._artifact_stage.endswith("_train")
                    else EVAL_NUM_GENERATIONS
                ),
            }
            records.append(record)
        with self._artifact_path.open("a", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        return output


def _trainable_fingerprint(model) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    trainable = 0
    total = 0
    for name, parameter in model.named_parameters():
        total += parameter.numel()
        if not parameter.requires_grad:
            continue
        trainable += parameter.numel()
        digest.update(name.encode())
        raw = parameter.detach().contiguous().view(torch.uint8).cpu().numpy().tobytes()
        digest.update(raw)
    return "sha256:" + digest.hexdigest(), trainable, total


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def summarize(path: Path) -> dict[str, Any]:
    rows = _records(path)
    failure_counts = Counter(row["failure_category"] for row in rows if row["failure_category"])
    summary = {
        "rollouts": len(rows),
        "unique_tasks": len({row["task"]["task_id"] for row in rows}),
        "success_count": sum(row["success"] for row in rows),
        "success_rate": statistics.fmean(row["success"] for row in rows),
        "verified_count": sum(row["verified"] for row in rows),
        "verified_rate": statistics.fmean(row["verified"] for row in rows),
        "mean_reward": statistics.fmean(row["reward"]["total_reward"] for row in rows),
        "mean_steps": statistics.fmean(row["agent_steps"] for row in rows),
        "mean_tool_attempts": statistics.fmean(row["tool_attempts"] for row in rows),
        "mean_input_tokens": statistics.fmean(row["prompt_tokens"] for row in rows),
        "mean_output_tokens": statistics.fmean(row["completion_tokens"] for row in rows),
        "mean_policy_tokens": statistics.fmean(row["policy_tokens"] for row in rows),
        "no_progress_count": sum(
            row["termination_reason"] == "NO_PROGRESS" for row in rows
        ),
        "failure_categories": dict(sorted(failure_counts.items())),
        "reward_ablation": {
            name: statistics.fmean(
                row["reward_ablation"][name]["total_reward"] for row in rows
            )
            for name in ("outcome_only", "outcome_efficiency")
        },
        "reward_hacking_checks": {
            key: sum(bool(row["reward_hacking_checks"].get(key)) for row in rows)
            for key in (
                "correct_without_required_evidence",
                "premature_submission",
                "avoided_all_evidence_tools",
            )
        },
    }
    return summary


def _new_trainer(
    *,
    stage: str,
    artifact_path: Path,
    model,
    tokenizer,
    train_dataset: Dataset,
    max_steps: int,
    peft_config: LoraConfig | None = None,
    callbacks: list[TrainerCallback] | None = None,
) -> ArtifactGRPOTrainer:
    return ArtifactGRPOTrainer(
        model=model,
        processing_class=tokenizer,
        args=_grpo_args(stage, max_steps=max_steps),
        train_dataset=train_dataset,
        reward_funcs=None,
        environment_factory=AxiomRepositoryEnvironment,
        peft_config=peft_config,
        callbacks=callbacks,
        artifact_path=artifact_path,
        stage=stage,
    )


def baseline() -> None:
    set_seed(SEED)
    os.environ["AXIOM_RL_REWARD_CONFIG"] = "outcome_efficiency"
    model = _load_base_model()
    tokenizer = _load_tokenizer()
    trainer = _new_trainer(
        stage="baseline_dev",
        artifact_path=RESULTS_DIR / "baseline_dev_trajectories.jsonl",
        model=model,
        tokenizer=tokenizer,
        train_dataset=load_dataset("train", limit=2),
        max_steps=1,
    )
    dev_metrics = trainer.evaluate(
        eval_dataset=load_dataset("dev"), metric_key_prefix="baseline_dev"
    )
    trainer._artifact_stage = "baseline_heldout"
    trainer._artifact_path = RESULTS_DIR / "baseline_heldout_trajectories.jsonl"
    if trainer._artifact_path.exists():
        raise FileExistsError(f"refusing to overwrite frozen artifact: {trainer._artifact_path}")
    heldout_metrics = trainer.evaluate(
        eval_dataset=load_dataset("heldout"), metric_key_prefix="baseline_heldout"
    )
    result = {
        "stage": "baseline",
        "runtime": _runtime_fingerprint(),
        "config": _experiment_config(max_steps=0, reward="outcome_efficiency"),
        "dev": summarize(RESULTS_DIR / "baseline_dev_trajectories.jsonl"),
        "heldout": summarize(RESULTS_DIR / "baseline_heldout_trajectories.jsonl"),
        "trainer_dev_metrics": dev_metrics,
        "trainer_heldout_metrics": heldout_metrics,
    }
    _json_dump(RESULTS_DIR / "baseline_metrics.json", result)
    print(json.dumps(result, indent=2, sort_keys=True))


def _replay_artifact(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite rescored artifact: {destination}")
    rows = _records(source)
    rescored = []
    for row in rows:
        environment = AxiomRepositoryEnvironment()
        environment.reset(**row["task"])
        for call in row["tool_calls"]:
            name = call["tool_name"]
            arguments = call["arguments"]
            if name == "search_repository":
                environment.search_repository(str(arguments["pattern"]))
            elif name == "read_repository_file":
                environment.read_repository_file(str(arguments["path"]))
            elif name == "submit_answer":
                environment.submit_answer(
                    str(arguments["file_path"]), str(arguments["symbol"])
                )
            else:
                raise ValueError(f"unexpected tool in baseline artifact: {name}")
        environment.get_reward()
        updated = environment._artifact_record(
            prompt_token_ids=row["prompt_token_ids"],
            completion_token_ids=row["completion_token_ids"],
            completion_mask=row["completion_mask"],
            tool_mask=row["loss_mask"],
            completion_text=row["completion_text"],
            advantage=row["advantage"],
            stage=row["stage"],
        )
        for key in ("model_id", "dataset_fingerprint", "sampling"):
            updated[key] = row[key]
        rescored.append(updated)
    with destination.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rescored:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def rescore_baseline() -> None:
    os.environ["AXIOM_RL_REWARD_CONFIG"] = "outcome_efficiency"
    old_metrics_path = RESULTS_DIR / "baseline_metrics_pre_reward_fix.json"
    old_metrics = json.loads(old_metrics_path.read_text(encoding="utf-8"))
    for split in ("dev", "heldout"):
        _replay_artifact(
            RESULTS_DIR / f"baseline_{split}_trajectories_pre_reward_fix.jsonl",
            RESULTS_DIR / f"baseline_{split}_trajectories.jsonl",
        )
    result = {
        **old_metrics,
        "config": _experiment_config(max_steps=0, reward="outcome_efficiency"),
        "dev": summarize(RESULTS_DIR / "baseline_dev_trajectories.jsonl"),
        "heldout": summarize(RESULTS_DIR / "baseline_heldout_trajectories.jsonl"),
        "reward_rescore_note": (
            "Same frozen model outputs and token captures; deterministic Axiom reward was "
            "rescored before nonzero-gradient training so only useful searches/reads receive "
            "the small process component."
        ),
    }
    _json_dump(RESULTS_DIR / "baseline_metrics.json", result)
    print(json.dumps(result, indent=2, sort_keys=True))


def _train(stage: str, *, max_steps: int, train_limit: int | None) -> dict[str, Any]:
    set_seed(SEED)
    os.environ["AXIOM_RL_REWARD_CONFIG"] = "outcome_efficiency"
    model = _load_base_model()
    tokenizer = _load_tokenizer()
    callback = GradientAuditCallback()
    artifact_path = RESULTS_DIR / f"{stage}_train_trajectories.jsonl"
    torch.cuda.reset_peak_memory_stats()
    trainer = _new_trainer(
        stage=f"{stage}_train",
        artifact_path=artifact_path,
        model=model,
        tokenizer=tokenizer,
        train_dataset=load_dataset("train", limit=train_limit),
        max_steps=max_steps,
        peft_config=_lora_config(),
        callbacks=[callback],
    )
    initial_hash, trainable, total = _trainable_fingerprint(trainer.model)
    train_result = trainer.train()
    final_hash, final_trainable, final_total = _trainable_fingerprint(trainer.model)
    checkpoint_dir = CHECKPOINTS_DIR / f"{stage}-adapter"
    trainer.save_model(checkpoint_dir)
    proof = {
        "stage": stage,
        "optimizer_global_steps": trainer.state.global_step,
        "pre_optimizer_callback_steps": callback.pre_optimizer_steps,
        "gradient_tensors_checked": callback.gradient_tensors,
        "nonfinite_gradient_tensors": callback.nonfinite_gradient_tensors,
        "train_loss": train_result.metrics.get("train_loss"),
        "finite_train_loss": math.isfinite(float(train_result.metrics.get("train_loss", math.nan))),
        "initial_trainable_fingerprint": initial_hash,
        "final_trainable_fingerprint": final_hash,
        "parameters_changed": initial_hash != final_hash,
        "trainable_parameters": trainable,
        "total_parameters": total,
        "final_trainable_parameters": final_trainable,
        "final_total_parameters": final_total,
        "peak_allocated_mib": round(torch.cuda.max_memory_allocated() / 2**20, 1),
        "peak_reserved_mib": round(torch.cuda.max_memory_reserved() / 2**20, 1),
        "checkpoint": str(checkpoint_dir.relative_to(ROOT)).replace("\\", "/"),
        "train_metrics": train_result.metrics,
        "rollouts": summarize(artifact_path),
        "config": _experiment_config(max_steps=max_steps, reward="outcome_efficiency"),
        "runtime": _runtime_fingerprint(),
    }
    _json_dump(RESULTS_DIR / f"{stage}_proof.json", proof)
    del trainer, model
    gc.collect()
    torch.cuda.empty_cache()

    reload_model = PeftModel.from_pretrained(_load_base_model(), checkpoint_dir)
    reloaded_adapter_files = sorted(
        path.name for path in checkpoint_dir.iterdir() if path.is_file()
    )
    proof["checkpoint_reload_succeeded"] = isinstance(reload_model, PeftModel)
    proof["checkpoint_files"] = reloaded_adapter_files
    _json_dump(RESULTS_DIR / f"{stage}_proof.json", proof)
    del reload_model
    gc.collect()
    torch.cuda.empty_cache()
    print(json.dumps(proof, indent=2, sort_keys=True))
    return proof


def smoke() -> None:
    proof = _train("smoke", max_steps=3, train_limit=3)
    required = (
        proof["optimizer_global_steps"] >= 1
        and proof["finite_train_loss"]
        and proof["parameters_changed"]
        and proof["checkpoint_reload_succeeded"]
        and proof["nonfinite_gradient_tensors"] == 0
    )
    if not required:
        raise SystemExit("smoke training did not prove a finite real parameter update")


def train() -> None:
    smoke_proof = json.loads((RESULTS_DIR / "smoke_proof.json").read_text(encoding="utf-8"))
    if not smoke_proof.get("parameters_changed"):
        raise SystemExit("successful smoke proof is required before actual training")
    proof = _train("trained", max_steps=TRAIN_STEPS, train_limit=None)
    if not proof["parameters_changed"]:
        raise SystemExit("actual training did not change trainable parameters")


def evaluate_trained() -> None:
    set_seed(SEED)
    os.environ["AXIOM_RL_REWARD_CONFIG"] = "outcome_efficiency"
    checkpoint_dir = CHECKPOINTS_DIR / "trained-adapter"
    model = PeftModel.from_pretrained(_load_base_model(), checkpoint_dir)
    tokenizer = _load_tokenizer()
    trainer = _new_trainer(
        stage="trained_dev",
        artifact_path=RESULTS_DIR / "trained_dev_trajectories.jsonl",
        model=model,
        tokenizer=tokenizer,
        train_dataset=load_dataset("train", limit=2),
        max_steps=1,
    )
    dev_metrics = trainer.evaluate(
        eval_dataset=load_dataset("dev"), metric_key_prefix="trained_dev"
    )
    trainer._artifact_stage = "trained_heldout"
    trainer._artifact_path = RESULTS_DIR / "trained_heldout_trajectories.jsonl"
    if trainer._artifact_path.exists():
        raise FileExistsError(f"refusing to overwrite frozen artifact: {trainer._artifact_path}")
    heldout_metrics = trainer.evaluate(
        eval_dataset=load_dataset("heldout"), metric_key_prefix="trained_heldout"
    )
    result = {
        "stage": "trained_evaluation",
        "runtime": _runtime_fingerprint(),
        "config": _experiment_config(max_steps=TRAIN_STEPS, reward="outcome_efficiency"),
        "dev": summarize(RESULTS_DIR / "trained_dev_trajectories.jsonl"),
        "heldout": summarize(RESULTS_DIR / "trained_heldout_trajectories.jsonl"),
        "trainer_dev_metrics": dev_metrics,
        "trainer_heldout_metrics": heldout_metrics,
    }
    _json_dump(RESULTS_DIR / "trained_metrics.json", result)
    print(json.dumps(result, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "stage",
        choices=("baseline", "rescore-baseline", "smoke", "train", "evaluate"),
    )
    args = parser.parse_args()
    if not torch.cuda.is_available():
        raise SystemExit("CUDA is required for this manual experiment")
    {
        "baseline": baseline,
        "rescore-baseline": rescore_baseline,
        "smoke": smoke,
        "train": train,
        "evaluate": evaluate_trained,
    }[args.stage]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
