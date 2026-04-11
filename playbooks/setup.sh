#!/usr/bin/env bash
# One-time setup for Neo4j Ansible deployments.
# Run from the repo root: ./playbooks/setup.sh
set -euo pipefail

echo "Installing Ansible..."
uv tool install ansible-core

# Ensure uv tool executables are on PATH for subsequent commands
export PATH="$HOME/.local/bin:$PATH"

echo "Installing Azure collection..."
ansible-galaxy collection install -r playbooks/requirements.yml

echo "Installing Azure collection Python dependencies..."
uv pip install --python "$(uv tool dir)/ansible-core/bin/python" \
  --prerelease=allow \
  -r ~/.ansible/collections/ansible_collections/azure/azcollection/requirements.txt

echo "Checking Azure login..."
if ! az account show &>/dev/null; then
  echo "Not logged in. Running az login..."
  az login
fi

echo "Subscription: $(az account show --query '[name, id]' -o tsv | tr '\t' ' ')"

echo "Accepting Neo4j Marketplace terms..."
az vm image terms accept --publisher neo4j --offer neo4j-ee-vm --plan byol

echo ""
echo "Setup complete. Next steps (from repo root):"
echo "  cd deployments && uv sync && uv run neo4j-deploy-ansible setup"
echo "  uv run neo4j-deploy-ansible deploy --scenario standalone-v2025"
