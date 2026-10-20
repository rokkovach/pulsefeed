#!/usr/bin/env bash
# Prepare the two AMD64 cloud images; never fetch model weights or create endpoints.
set -euo pipefail
export LC_ALL=C

usage() {
    cat <<'EOF'
Usage: bash scripts/runpod-images.sh --registry REGISTRY/NAMESPACE --version VERSION [--repository NAME] [--plan|--build|--push]

  --registry  Explicit lowercase registry host and namespace, e.g. docker.io/myteam
  --version   A new, never-reused version tag (not latest), e.g. v1.0.0-20261016
  --repository  Optional shared repository: NAME:qwen-VERSION and NAME:flux-VERSION
                Otherwise use pulsefeed-qwen:VERSION and pulsefeed-flux:VERSION
  --plan      Print the preparation/build/cleanup recipe without executing it (default)
  --build     Build both linux/amd64 images separately and load them locally
  --push      Build both linux/amd64 images separately and publish to the registry

Build/push use the currently selected Docker context and buildx builder. Configure
registry access separately; this script does not log in or change Docker settings.
Verify intended repository visibility before pushing; the current release is public.
This script cannot set visibility. Private images need read-only registry credentials.
Tags must be kept immutable by the operator/registry; record published digests.
EOF
}

fail() { printf 'runpod-images: %s\n' "$1" >&2; exit 2; }

registry=''
version=''
repository=''
mode='plan'
mode_set=false
while (($#)); do
    case "$1" in
        --registry)
            [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || fail '--registry requires REGISTRY/NAMESPACE.'
            [[ -z "$registry" ]] || fail '--registry may only be supplied once.'
            registry=$2
            shift 2
            ;;
        --version)
            [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || fail '--version requires a version tag.'
            [[ -z "$version" ]] || fail '--version may only be supplied once.'
            version=$2
            shift 2
            ;;
        --repository)
            [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || fail '--repository requires a single repository name.'
            [[ -z "$repository" ]] || fail '--repository may only be supplied once.'
            repository=$2
            shift 2
            ;;
        --plan|--build|--push)
            [[ "$mode_set" == false ]] || fail 'Choose exactly one of --plan, --build, or --push.'
            mode=${1#--}
            mode_set=true
            shift
            ;;
        --help|-h) usage; exit 0 ;;
        *) fail 'Unknown argument; use --help.' ;;
    esac
done

[[ -n "$registry" && -n "$version" ]] || fail '--registry and --version are required; use --help.'
# Deliberately accept a conservative subset of registry/repository syntax. No
# schemes, credentials, image tags, shell metacharacters, whitespace or options.
registry_pattern='^[a-z0-9]+([.-][a-z0-9]+)*(:[0-9]{1,5})?/[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*$'
[[ "$registry" =~ $registry_pattern ]] || fail 'Registry must be a lowercase host/namespace without credentials, a tag or whitespace.'
registry_host=${registry%%/*}
[[ "$registry_host" == *.* || "$registry_host" == *:* || "$registry_host" == localhost ]] || fail 'Use an explicit registry host, for example docker.io/myteam.'
[[ "$version" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || fail 'Version must be a Docker tag of 1–128 ASCII letters, digits, dots, underscores or hyphens, starting with a letter or digit.'
case "$version" in
    [Ll][Aa][Tt][Ee][Ss][Tt]) fail 'Use a new, never-reused version instead of latest.' ;;
esac

qwen_name=$registry/pulsefeed-qwen
flux_name=$registry/pulsefeed-flux
qwen_tag=$version
flux_tag=$version
if [[ -n "$repository" ]]; then
    [[ "$repository" =~ ^[a-z0-9]+([._-][a-z0-9]+)*$ ]] || fail 'Repository must be a single lowercase name using letters, digits and separated dots, underscores or hyphens.'
    qwen_name=$registry/$repository
    flux_name=$qwen_name
    qwen_tag=qwen-$version
    flux_tag=flux-$version
fi
[[ ${#qwen_name} -le 255 && ${#flux_name} -le 255 ]] || fail 'Final image repository names must be at most 255 characters; shorten --registry or --repository.'
[[ ${#qwen_tag} -le 128 && ${#flux_tag} -le 128 ]] || fail 'Final image tags must be at most 128 characters; with --repository, --version is limited to 123.'

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export_mode=--load
[[ "$mode" != push ]] || export_mode=--push
qwen=(docker buildx build --platform linux/amd64 --tag "$qwen_name:$qwen_tag" "$export_mode")
flux=(docker buildx build --platform linux/amd64 --target runtime --tag "$flux_name:$flux_tag" "$export_mode" "$repo_root/workers/flux")
prepare=(python3 "$repo_root/workers/qwen/prepare_build.py" --platform amd64 --output-dir)

print_command() { printf '%q ' "$@"; printf '\n'; }
print_context_command() { printf '%q ' "$@"; printf '"$build_root/qwen"\n'; }
if [[ "$mode" == plan ]]; then
    printf '# Plan only: --build runs this recipe; --push replaces --load with --push.\n'
    # The printed recipe deliberately retains its temporary-directory variable;
    # printing a guessed path would not produce an executable cleanup recipe.
    cat <<'RECIPE'
set -euo pipefail
build_root=$(mktemp -d "${TMPDIR:-/tmp}/pulsefeed-runpod.XXXXXXXX")
trap 'rm -rf -- "$build_root"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
RECIPE
    print_context_command "${prepare[@]}"
    print_context_command "${qwen[@]}"
    print_command "${flux[@]}"
    exit 0
fi

command -v docker >/dev/null 2>&1 || fail 'Docker is required for --build or --push.'
command -v python3 >/dev/null 2>&1 || fail 'Python 3 is required to prepare the Qwen build context.'
# prepare_build requires a nonexistent output directory. Keep it beneath a
# private mktemp parent so credentials/source trees cannot enter the context and
# cleanup removes only this invocation's disposable files, including on failure.
build_root=$(mktemp -d "${TMPDIR:-/tmp}/pulsefeed-runpod.XXXXXXXX")
trap 'rm -rf -- "$build_root"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
print_command "${prepare[@]}" "$build_root/qwen"
"${prepare[@]}" "$build_root/qwen"
print_command "${qwen[@]}" "$build_root/qwen"
"${qwen[@]}" "$build_root/qwen"
print_command "${flux[@]}"
"${flux[@]}"
