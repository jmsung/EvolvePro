#!/usr/bin/env python3 -u
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import os
import argparse
import pathlib
import pandas as pd
import torch

from esm import FastaBatchedDataset, pretrained, MSATransformer


def create_parser():
    parser = argparse.ArgumentParser(
        description="Extract per-token representations and model outputs for sequences in a FASTA file"  # noqa
    )

    parser.add_argument(
        "model_location",
        type=str,
        help="PyTorch model file OR name of pretrained model to download (see README for models)",
    )
    parser.add_argument(
        "fasta_file",
        type=pathlib.Path,
        help="FASTA file on which to extract representations",
    )
    parser.add_argument(
        "output_dir",
        type=pathlib.Path,
        help="output directory for extracted representations",
    )

    parser.add_argument(
        "--toks_per_batch", type=int, default=4096, help="maximum batch size"
    )
    parser.add_argument(
        "--repr_layers",
        type=int,
        default=[-1],
        nargs="+",
        help="layers indices from which to extract representations (0 to num_layers, inclusive)",
    )
    parser.add_argument(
        "--include",
        type=str,
        nargs="+",
        choices=["mean", "per_tok", "bos", "contacts"],
        help="specify which representations to return",
        required=True,
    )
    parser.add_argument(
        "--truncation_seq_length",
        type=int,
        default=1022,
        help="truncate sequences longer than the given value",
    )

    parser.add_argument(
        "--nogpu", action="store_true", help="Do not use GPU even if available"
    )

    parser.add_argument(
        "--concatenate_dir",
        type=pathlib.Path,
        default=None,
        help="output directory for concatenated representations",
    )

    return parser


def get_device():
    """Get the best available device (MPS > CUDA > CPU)"""
    if torch.backends.mps.is_available():
        return torch.device("mps")
    elif torch.cuda.is_available():
        return torch.device("cuda")
    else:
        return torch.device("cpu")


def run(args):
    model, alphabet = pretrained.load_model_and_alphabet(args.model_location)
    model.eval()
    if isinstance(model, MSATransformer):
        raise ValueError(
            "This script currently does not handle models with MSA input (MSA Transformer)."
        )
    
    # Get the best available device
    device = get_device()
    if not args.nogpu and device.type != "cpu":
        model = model.to(device)
        print(f"Transferred model to {device}")

    dataset = FastaBatchedDataset.from_file(args.fasta_file)
    batches = dataset.get_batch_indices(args.toks_per_batch, extra_toks_per_seq=1)
    data_loader = torch.utils.data.DataLoader(
        dataset, collate_fn=alphabet.get_batch_converter(), batch_sampler=batches
    )
    print(f"Read {args.fasta_file} with {len(dataset)} sequences")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    return_contacts = "contacts" in args.include

    assert all(
        -(model.num_layers + 1) <= i <= model.num_layers for i in args.repr_layers
    )
    repr_layers = [
        (i + model.num_layers + 1) % (model.num_layers + 1) for i in args.repr_layers
    ]

    with torch.no_grad():
        for batch_idx, (labels, strs, toks) in enumerate(data_loader):
            print(
                f"Processing {batch_idx + 1} of {len(batches)} batches ({toks.size(0)} sequences)"
            )
            if not args.nogpu and device.type != "cpu":
                toks = toks.to(device=device, non_blocking=True)

            print(f"Device: {toks.device}")

            out = model(toks, repr_layers=repr_layers, return_contacts=return_contacts)

            logits = out["logits"].to(device="cpu")
            representations = {
                layer: t.to(device="cpu") for layer, t in out["representations"].items()
            }
            if return_contacts:
                contacts = out["contacts"].to(device="cpu")

            for i, label in enumerate(labels):
                args.output_file = args.output_dir / f"{label}.pt"
                args.output_file.parent.mkdir(parents=True, exist_ok=True)
                result = {"label": label}
                truncate_len = min(args.truncation_seq_length, len(strs[i]))
                # Call clone on tensors to ensure tensors are not views into a larger representation
                # See https://github.com/pytorch/pytorch/issues/1995
                if "per_tok" in args.include:
                    result["representations"] = {
                        layer: t[i, 1 : truncate_len + 1].clone()
                        for layer, t in representations.items()
                    }
                if "mean" in args.include:
                    result["mean_representations"] = {
                        layer: t[i, 1 : truncate_len + 1].mean(0).clone()
                        for layer, t in representations.items()
                    }
                if "bos" in args.include:
                    result["bos_representations"] = {
                        layer: t[i, 0].clone() for layer, t in representations.items()
                    }
                if return_contacts:
                    result["contacts"] = contacts[
                        i, :truncate_len, :truncate_len
                    ].clone()

                torch.save(
                    result,
                    args.output_file,
                )

    print(f"Saved representations to {args.output_dir}")


def concatenate_files(output_dir, output_csv):
    # Get all .pt files in the output directory
    files = []
    for r, d, f in os.walk(output_dir):
        for file in f:
            if ".pt" in file:
                files.append(os.path.join(r, file))

    # Load each file and append to a list of dataframes
    dataframes = []
    for file_path in files:
        file_data = torch.load(file_path)
        label = file_data["label"]
        representations = file_data["mean_representations"]
        key, tensor = representations.popitem()
        row_name = label
        row_data = tensor.tolist()
        new_df = pd.DataFrame([row_data], index=[row_name])
        dataframes.append(new_df)

    # Concatenate all dataframes
    if dataframes:
        concatenated_df = pd.concat(dataframes)
        print("Shape of concatenated DataFrame:", concatenated_df.shape)
        concatenated_df.to_csv(output_csv)
        print(f"Saved concatenated representations to {output_csv}")
    else:
        print("No data to concatenate.")


def main():
    parser = create_parser()
    args = parser.parse_args()

    run(args)

    if args.concatenate_dir is not None:
        fasta_file_name = args.fasta_file.stem
        output_csv = (
            f"{args.concatenate_dir}/{fasta_file_name}_{args.model_location}.csv"
        )
        concatenate_files(args.output_dir, output_csv)
        # print(f"Removing {args.output_dir}")
        # shutil.rmtree(args.output_dir)
    else:
        print(
            "Skipping concatenation, file move, and cleanup as --concatenate_dir flag was not set."
        )


if __name__ == "__main__":
    main()
