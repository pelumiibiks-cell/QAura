"""Auto-configuration: analyze a live site and propose a qaura.yaml for it.

Entry point is cli.py's `init` command, which drives recon -> inference -> emission.
Nothing here ever writes qaura.yaml; the output is always a separate, reviewable file.
"""
