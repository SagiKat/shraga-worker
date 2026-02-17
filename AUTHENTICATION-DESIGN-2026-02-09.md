# Shraga Authentication Design

**Date:** February 9, 2026
**Purpose:** Complete authentication design for Shraga system (Orchestrator + Workers)
**Status:** Design Complete - Ready for Implementation

---

## Overview

Shraga requires authentication for three separate operations:
1. **Dataverse API access** - Task communication (orchestrator + workers)
2. **Claude Code CLI** - Task execution (workers only)
3. **Azure Dev Box provisioning** - Creating user Dev Boxes (orchestrator only)

---

## Design Principles

1. **Hybrid approach**: Device code flow first, RDP browser fallback
2. **User-driven**: Wait for user input, not arbitrary timeouts
3. **Per-user identity**: Each dev authenticates with their own credentials
4. **Team-friendly**: Easy setup for sharing with team members
5. **Minimal friction**: One-time setup per Dev Box

---

## Authentication Requirements Matrix

| Component | Needs Auth For | Identity | Method |
|-----------|---------------|----------|---------|
| **Orchestrator** | Dataverse API | Admin | DefaultAzureCredential |
| **Orchestrator** | Dev Box Provisioning | Admin | DefaultAzureCredential |
| **Worker** | Dataverse API | User | DefaultAzureCredential |
| **Worker** | Claude Code CLI | User | Device code / Browser |

---

## 1. Dataverse Authentication

### Technology: Azure DefaultAzureCredential

**Why:**
- Automatic token refresh
- No manual credential management
- Uses existing `az login` credentials
- Secure (no tokens in config files)

### Setup Flow (One-time per Dev Box)

**Device Code Flow (Primary)**:
1. Worker/Orchestrator detects not authenticated
2. Sends Teams message with device code URL
3. User opens URL on their device (phone/laptop)
4. User signs in, receives code
5. User replies with code in Teams
6. Auth completes, credentials cached

**RDP Browser Flow (Fallback)**:
1. If device code fails/times out
2. Send Teams message with RDP link
3. User connects via browser to Dev Box
4. User runs `az login` in terminal
5. Browser opens on Dev Box, user signs in
6. Done

### Implementation Pattern

```python
from azure.identity import DefaultAzureCredential

# Worker/Orchestrator initialization
credential = DefaultAzureCredential()
token = credential.get_token(f"{DATAVERSE_URL}/.default")

# DefaultAzureCredential tries in order:
# 1. Environment variables (service principal)
# 2. Managed Identity (if Dev Box has one)
# 3. Azure CLI credentials (from az login) <- We use this
# 4. Visual Studio credentials
# 5. Interactive browser (last resort)
```

### Token Management
- Token valid for ~1 hour
- DefaultAzureCredential auto-refreshes
- Worker caches token to avoid redundant calls
- No manual expiry handling needed

---

## 2. Claude Code Authentication

### Technology: Device code flow + Teams messaging

**Reference**: See `AUTHENTICATION-GUIDE.md` and `claude_auth_teams.py`

### Setup Flow (One-time per Dev Box)

**Device Code Flow (Primary)**:
1. Worker runs `claude /login`
2. Captures auth URL from CLI output
3. Sends Teams message to user with URL
4. User opens URL on their device
5. User signs in, receives code
6. User replies with code in Teams
7. Worker submits code to Claude CLI stdin
8. Auth completes

**RDP Browser Flow (Fallback)**:
1. If device code fails
2. Send RDP link to user
3. User connects to Dev Box
4. User runs `claude /login` manually
5. Browser opens on Dev Box
6. User completes auth in browser
7. Done

### Implementation
- Use `claude_auth_teams.py` (already implemented)
- Integrates with Teams bot for user communication
- Handles code submission to CLI

---

## 3. Dev Box Provisioning Authentication

### Used By: Orchestrator only

### Option 1: Admin's Credentials (Recommended)

**Setup:**
- Admin authenticates orchestrator Dev Box via `az login`
- Uses same device code + RDP fallback pattern
- DefaultAzureCredential picks up admin's credentials

**Orchestrator can then:**
- Call Azure Dev Box REST APIs
- Provision user Dev Boxes
- Monitor provisioning status

**Pros:**
- Simple, consistent with other auth
- No additional setup needed
- Admin identity = clear audit trail

**Cons:**
- Requires admin's Azure CLI login to remain valid
- All provisioning done as admin user

### Option 2: Service Principal (Alternative)

**Setup:**
- Create app registration in Azure AD
- Grant Dev Box provisioning permissions
- Store client ID + secret in orchestrator config

**Pros:**
- Dedicated identity for automation
- Independent of admin's login

**Cons:**
- Additional setup required
- Shared secret management

**Decision:** Start with Option 1, migrate to Option 2 if needed

---

## Communication Architecture

### Dataverse for Task Queue (Keep existing POC pattern)

**Why:**
- Proven, already working
- Simple polling-based communication
- Orchestrator writes tasks → Workers poll

**Fix required:** Replace insecure subprocess auth with DefaultAzureCredential

### Git for Results Versioning (New addition)

**Why Git is GOOD for results:**
- Version control of all changes
- Audit trail of what Claude did
- Easy to review/rollback
- Git already authenticated on Dev Boxes

**Flow:**
1. Worker completes task
2. Commits entire working directory to Git
3. Updates Dataverse with commit SHA
4. Results preserved and reviewable

**Why Git is BAD for task queue:**
- Too much overhead (commit/push/pull cycles)
- Designed for code, not rapid messaging
- Dataverse is better for queue pattern

---

## Security Issues Fixed

### Original Problem (in `integrated_task_worker.py` lines 63-79)

1. **Command injection vulnerability**
   - Line 74: `shell=True` with subprocess
   - Risk: Malicious input in DATAVERSE_URL could execute commands

2. **Hardcoded paths**
   - Line 68: Hardcoded Azure CLI path
   - Breaks if CLI installed elsewhere

3. **No token caching**
   - Called 4 times per task cycle
   - Wasteful subprocess spawns

4. **Token exposure**
   - Tokens in exception messages
   - Risk of leaking in logs

### Solution: DefaultAzureCredential

- No subprocess calls
- Built-in token caching
- Secure token handling
- Portable across environments

---

## Team Rollout Plan

### For Each Dev Team Member:

**One-time setup (5 minutes)**:

1. **Provision Dev Box**
   - Admin triggers provisioning
   - Dev Box created with customization YAML

2. **First startup prompts**:
   - Dataverse auth: Device code → Teams message → User replies
   - Claude Code auth: Device code → Teams message → User replies
   - Fallback: RDP link provided if needed

3. **Done!**
   - Worker polls Dataverse for tasks
   - Executes with Claude Code
   - Commits results to Git

### For Admin (Orchestrator):

**One-time setup (5 minutes)**:

1. **Authenticate orchestrator Dev Box**:
   - Device code for Dataverse
   - Device code for Dev Box provisioning APIs

2. **Start orchestrator**:
   - Runs as background process
   - Polls Dataverse for new tasks
   - Provisions Dev Boxes as needed

---

## Dev Box Customization Integration

### File: `devbox-customization-shraga.yaml`

**User tasks section** (runs after first sign-in):

1. Create OneDrive Shraga folder structure (for testing/fallback)
2. Clone shraga-worker repository
3. Install Python dependencies
4. Prompt for authentication (device code)
5. Verify installations

**Important:** No admin-level tasks (HKLM registry, etc.) - user can't do admin operations

---

## User Experience Flow

### First Time Using Shraga:

**User submits task via Teams**:
1. Copilot Studio collects task details
2. Writes to Dataverse

**Orchestrator discovers task**:
1. Creates admin mirror
2. Checks if user has Dev Box
3. If no → Provisions new Dev Box
4. Sends Teams message: "Your Dev Box is being created..."

**Dev Box provisioned**:
1. Customization runs on first startup
2. Prompts user for auth (via Teams)
3. User authenticates from their device
4. Worker starts automatically

**Task execution**:
1. Worker picks up assigned task
2. Executes with Claude Code
3. Streams progress to Teams
4. Commits results to Git
5. Updates Dataverse

**User receives**:
1. Real-time progress in Teams
2. Final results with Git commit link
3. Can review changes in Git history

---

## Next Steps for Implementation

1. **Fix Dataverse auth in `integrated_task_worker.py`**
   - Replace subprocess with DefaultAzureCredential
   - Add token caching
   - Remove hardcoded paths

2. **Implement device code auth flow**
   - Teams messaging integration
   - User input handling
   - Fallback to RDP link

3. **Add Git results committing**
   - Post-task commit logic
   - Include commit SHA in Dataverse
   - Clean commit messages

4. **Update orchestrator for Dev Box provisioning**
   - Add Azure Dev Box API calls
   - Monitor provisioning status
   - Notify users when ready

5. **Test with team**
   - Validate device code flow
   - Test RDP fallback
   - Verify Git commits work

---

## Files Reference

- `shraga-architecture-2026-02-06.md` - Overall system architecture
- `AUTHENTICATION-GUIDE.md` - Claude Code auth options
- `claude_auth_teams.py` - Teams-based Claude auth implementation
- `devbox-customization-shraga.yaml` - Dev Box setup automation
- `integrated_task_worker.py` - Worker implementation (needs auth fix)

---

## Decision Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Dataverse auth method | DefaultAzureCredential | Secure, automatic, no manual tokens |
| Claude Code auth method | Device code + Teams | No RDP needed, works from any device |
| Provisioning auth method | Admin's az login | Simple, consistent, start with this |
| Auth flow pattern | Device code first, RDP fallback | Best UX, covers all scenarios |
| Task communication | Dataverse (keep POC) | Proven, simple, works |
| Results sharing | Git commits | Perfect for version control, audit trail |
| User experience | Interactive via Teams | Natural, familiar interface |

---

**End of Document**
