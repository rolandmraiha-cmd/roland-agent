"""DPO reference is the SFT-merged model; never the pre-SFT base."""

from training.common import config, load_jsonl


def train(base, data, out, *, dry_run=False):
    import torch
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, EarlyStoppingCallback
    from trl import DPOConfig, DPOTrainer

    from training.check_gpu import check

    if not dry_run:
        check()
    settings = config()
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
    tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True)
    rows = load_jsonl(data / "dpo.jsonl")
    split = max(1, min(len(rows) - 1, round(len(rows) * 0.9)))
    interval = 1 if dry_run else max(1, min(25, split // 32))
    trainer = DPOTrainer(
        model=model,
        ref_model=None,
        processing_class=tokenizer,
        peft_config=LoraConfig(
            r=16,
            lora_alpha=32,
            lora_dropout=0.05,
            target_modules=settings["sft"]["target_modules"],
            task_type="CAUSAL_LM",
        ),
        args=DPOConfig(
            output_dir=str(out),
            learning_rate=settings["dpo"]["learning_rate"],
            num_train_epochs=1,
            max_steps=1 if dry_run else -1,
            beta=0.1,
            seed=42,
            report_to="none",
            max_length=512 if dry_run else 4096,
            per_device_train_batch_size=1,
            gradient_accumulation_steps=1 if dry_run else 16,
            bf16=not dry_run,
            use_cpu=dry_run,
            eval_strategy="steps",
            eval_steps=interval,
            save_strategy="steps",
            save_steps=interval,
            save_total_limit=2,
            load_best_model_at_end=True,
            metric_for_best_model="eval_loss",
            greater_is_better=False,
            disable_tqdm=True,
        ),
        train_dataset=Dataset.from_list(rows[:split]),
        eval_dataset=Dataset.from_list(rows[split:]),
        callbacks=[EarlyStoppingCallback(early_stopping_patience=2)],
    )
    result = trainer.train()
    trainer.save_model(str(out / "adapter"))
    return result.metrics
