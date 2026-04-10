# Questions to Ask the Customer

| # | Question | Why It Matters |
|---|----------|----------------|
| 1 | Was your Databricks workspace deployed with VNet injection (do you see a VNet you own listed under the workspace Properties in the portal)? | If no, peering is not possible on the existing workspace — it must be recreated. This is the single most important answer before any other work begins. |
| 2 | Are the Neo4j cluster and the Databricks workspace in the same Azure subscription? | Cross-subscription peering requires additional role assignments on the remote VNet. Same subscription is straightforward. |
| 3 | Are they in the same Azure region? | Cross-region (global) VNet peering is supported but incurs cost. Same region is free and lower latency. |
| 4 | What is the address space of the Neo4j VNet, and what is the address space of the Databricks VNet (if VNet-injected)? | The two address spaces must not overlap. Peering fails if they do, and the only fix is to recreate a VNet with a non-overlapping CIDR. |
| 5 | Does the deploying identity have Network Contributor (or equivalent) on both VNets? | Peering requires write access on both sides. Missing permissions on either side will cause the deployment to fail partway through. |
