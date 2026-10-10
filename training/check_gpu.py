"""A full run requires a separate CUDA machine with at least 16 GiB VRAM."""


def check():
    import torch

    if not torch.cuda.is_available() or torch.cuda.get_device_properties(0).total_memory < 16 * 1024**3:
        raise SystemExit(3)


if __name__ == "__main__":
    check()
