"""Merge only locally verified base and adapter weights; no remote code."""


def merge(base, adapter, out, *, dry_run=False):
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    model = AutoModelForCausalLM.from_pretrained(
        base,
        local_files_only=True,
        trust_remote_code=False,
        torch_dtype=torch.float32 if dry_run else torch.bfloat16,
    )
    model = PeftModel.from_pretrained(model, adapter, local_files_only=True).merge_and_unload()
    model.save_pretrained(out, safe_serialization=True)
    AutoTokenizer.from_pretrained(base, local_files_only=True).save_pretrained(out)
