"""QLoRA SFT on a separate GPU; completion-only loss, including earlier assistant context."""

from __future__ import annotations

from pathlib import Path

from training.common import config, load_jsonl


def train(base: Path, data: Path, out: Path, *, dry_run=False) -> dict:
    import torch
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, EarlyStoppingCallback
    from trl import SFTConfig, SFTTrainer

    settings = config()["sft"]
    train_rows = load_jsonl(data / "train.jsonl")
    interval = (
        1 if dry_run else max(1, min(25, len(train_rows) // (settings["gradient_accumulation_steps"] * 2)))
    )
    if not dry_run:
        from training.check_gpu import check

        check()
    tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True)
    tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        base,
        local_files_only=True,
        trust_remote_code=False,
        torch_dtype=torch.float32 if dry_run else torch.bfloat16,
        quantization_config=None
        if dry_run
        else BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
        ),
    )
    arguments = SFTConfig(
        output_dir=str(out),
        learning_rate=settings["learning_rate"],
        num_train_epochs=settings["epochs"],
        max_steps=1 if dry_run else -1,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=1 if dry_run else settings["gradient_accumulation_steps"],
        warmup_ratio=settings["warmup_ratio"],
        lr_scheduler_type="cosine",
        seed=42,
        max_length=512 if dry_run else config()["max_seq_len"],
        completion_only_loss=True,
        bf16=not dry_run,
        use_cpu=dry_run,
        report_to="none",
        eval_strategy="steps",
        eval_steps=interval,
        save_strategy="steps",
        save_steps=interval,
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        logging_steps=1,
        disable_tqdm=True,
    )
    trainer = SFTTrainer(
        model=model,
        args=arguments,
        processing_class=tokenizer,
        train_dataset=Dataset.from_list(train_rows),
        eval_dataset=Dataset.from_list(load_jsonl(data / "val.jsonl")),
        peft_config=LoraConfig(
            r=settings["lora_r"],
            lora_alpha=settings["lora_alpha"],
            lora_dropout=settings["lora_dropout"],
            target_modules=settings["target_modules"],
            task_type="CAUSAL_LM",
        ),
        callbacks=[EarlyStoppingCallback(early_stopping_patience=2)],
    )
    result = trainer.train()
    trainer.save_model(str(out / "adapter"))
    tokenizer.save_pretrained(out / "adapter")
    return result.metrics
