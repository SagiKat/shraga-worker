# Environment variables for Shraga Worker
# Copy this to config.sh and fill in your values (config.sh is gitignored)

export DATAVERSE_URL="https://org3e79cdb1.crm3.dynamics.com"
export TABLE_NAME="cr_shraga_tasks"
export WEBHOOK_USER="your-email@microsoft.com"
export WORK_BASE_DIR="/path/to/agent/work/directory"  # Optional, defaults to script directory
export UPDATE_BRANCH="origin/users/sagik/shraga-worker"  # Optional, git branch for auto-updates
