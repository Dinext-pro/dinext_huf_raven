"""Prepare an editable Huf installation view for a Frappe v15 bench.

Run with the bench Python. The view references the pinned checkout's sources and
keeps vector backends opt-in. The editable package uses this stable directory.
"""
import argparse
from importlib import metadata
from pathlib import Path
import subprocess
import sys

HUF_COMMIT = "ea6b0df3fb55e73376e1e1471b34ccb94750766e"
VECTOR_DEPS = (
    "llama-index-core", "sqlite-vec", "pysqlite3-binary", "chromadb",
    "llama-index-vector-stores-chroma", "llama-index-vector-stores-postgres",
    "llama-index-vector-stores-weaviate", "llama-index-vector-stores-pinecone",
    "redisvl", "zvec", "llama-index-vector-stores-faiss", "faiss-cpu",
)


def build_profile(source, view, use_bench_litellm=False):
    revision = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True).strip()
    if revision != HUF_COMMIT:
        raise ValueError(f"Expected Huf {HUF_COMMIT}, found {revision}")
    original = subprocess.check_output(["git", "-C", source, "show", "HEAD:pyproject.toml"], text=True)
    lines = original.splitlines(keepends=True)
    optional = []
    for name in VECTOR_DEPS:
        candidates = [line for line in lines if line.lstrip().startswith(
            (f'"{name}"', f'"{name}>', f'"{name};'))]
        if len(candidates) != 1:
            raise ValueError(f"Expected one dependency declaration for {name}")
        optional.extend(candidates)
        lines.remove(candidates[0])
    marker = lines.index("[project.optional-dependencies]\n") + 1
    lines[marker:marker] = ["vector-backends = [\n", *optional, "]\n"]
    if use_bench_litellm:
        from packaging.requirements import Requirement

        provider_line = next(line for line in lines if line.lstrip().startswith('"litellm>'))
        provider = Requirement(provider_line.strip().rstrip(",").strip('"'))
        installed = metadata.version("litellm")
        if installed not in provider.specifier:
            raise ValueError(f"The installed LiteLLM {installed} is outside Huf's supported range")
        lines.remove(provider_line)
        marker = lines.index("[project.optional-dependencies]\n") + 1
        lines[marker:marker] = ["provider-dependencies = [\n", provider_line, "]\n"]
    view.mkdir(parents=True, exist_ok=True)
    manifest = view / "pyproject.toml"
    if not manifest.exists() and any(view.iterdir()):
        raise ValueError("The installation view must start empty")
    for entry in source.iterdir():
        if entry.name.startswith(".") or entry.name == "pyproject.toml":
            continue
        linked = view / entry.name
        if not linked.exists():
            linked.symlink_to(entry)
        if linked.resolve() != entry.resolve():
            raise ValueError(f"Unexpected installation view entry: {linked.name}")
    manifest.write_text("".join(lines))


def write_constraints(path, use_bench_litellm):
    from packaging.requirements import Requirement

    requirements = [Requirement(raw) for raw in metadata.requires("frappe") or ()]
    pyproject_pins = [str(req) for req in requirements if not req.url]
    if not use_bench_litellm:
        # Resolve the provider against Frappe on a bench without a shared LiteLLM.
        pyproject_pins.append("litellm==1.83.0")
    path.write_text("\n".join([*pyproject_pins, ""]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("huf_checkout", type=Path)
    parser.add_argument("--view", type=Path, default=Path(sys.prefix) / "share" / "dinext-huf-v15")
    parser.add_argument("--constraints", type=Path)
    parser.add_argument("--use-bench-litellm", action="store_true",
                        help="Use a supported LiteLLM version already managed by another bench app")
    args = parser.parse_args()
    build_profile(args.huf_checkout.resolve(), args.view.resolve(), args.use_bench_litellm)
    if args.constraints:
        write_constraints(args.constraints, args.use_bench_litellm)
    print(args.view.resolve())
