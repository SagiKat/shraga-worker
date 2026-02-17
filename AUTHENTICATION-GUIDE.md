# Claude Code Authentication Options for Shraga

This guide compares all authentication approaches for Claude Code on Dev Box workers.

## Summary of Options

| Option | User Experience | Complexity | Security | Recommended |
|--------|----------------|------------|----------|-------------|
| **A. Teams Code Exchange** | Simplest - user clicks link on their device, sends code back | Low | High | ✅ **YES - MVP** |
| B. Credential File Transfer | User authenticates locally, uploads file | Medium | Medium | ⚠️ Fallback |
| C. Kiosk Browser (RDP) | User connects to Dev Box, sees fullscreen browser | High | High | ❌ No (doesn't work with copy-paste flow) |
| D. Regular Browser (RDP) | User connects to Dev Box, uses full desktop | Medium | High | ⚠️ If Teams fails |

---

## Option A: Teams Code Exchange ⭐ RECOMMENDED

### How It Works

```
┌──────────────────────────────────────────────────────────────┐
│ 1. Orchestrator (on Dev Box)                                 │
│    Runs: claude /login                                       │
└────────────┬─────────────────────────────────────────────────┘
             │
             ↓ Captures auth URL
┌────────────────────────────────────────────────────────────┐
│ 2. Bot sends Teams message                                  │
│    "Click here: https://auth.anthropic.com/device/abc123..." │
└────────────┬───────────────────────────────────────────────┘
             │
             ↓ User clicks
┌────────────────────────────────────────────────────────────┐
│ 3. User's Device (phone, laptop, etc.)                      │
│    Opens Anthropic sign-in page                             │
│    User signs in                                             │
│    Browser shows: "Your code is: ABC-123-XYZ"               │
└────────────┬───────────────────────────────────────────────┘
             │
             ↓ User copies code
┌────────────────────────────────────────────────────────────┐
│ 4. User replies in Teams                                    │
│    "ABC-123-XYZ"                                             │
└────────────┬───────────────────────────────────────────────┘
             │
             ↓ Bot receives code
┌────────────────────────────────────────────────────────────┐
│ 5. Bot sends code to Claude CLI stdin                       │
│    Authentication complete! ✅                               │
└──────────────────────────────────────────────────────────────┘
```

### Pros
- ✅ **Simplest user experience** - just click link and send code
- ✅ **User stays on their device** - no need to connect to Dev Box
- ✅ **Works on any device** - phone, laptop, tablet
- ✅ **Secure** - user authenticates with their own credentials
- ✅ **Natural Teams flow** - familiar messaging interface
- ✅ **No file transfer needed**
- ✅ **No RDP session needed**

### Cons
- ⚠️ Requires bot to capture CLI output and send to stdin
- ⚠️ User must have Teams access

### Implementation

```python
from claude_auth_teams import TeamsClaudeAuth

# Initialize
auth = TeamsClaudeAuth(send_teams_message, user_id)

# Request authentication
auth.request_authentication()  # Sends URL to user

# Later, when user replies with code via Teams:
auth.handle_user_code(user_code)  # Submits code to CLI
```

### User Message Example

```
🔐 Claude Code Authentication Required

Please authenticate to enable your Dev Box worker:

Steps:
1. Click this link: https://auth.anthropic.com/device/abc123...
2. Sign in with your Anthropic account
3. Copy the code shown (e.g., "ABC-123-XYZ")
4. Reply with the code in this chat

⏱️ Waiting for your code...
```

---

## Option B: Credential File Transfer

### How It Works

```
1. User runs `claude /login` on THEIR local machine
2. User completes authentication (copy-paste code flow)
3. Credential file created: ~/.config/claude-code/auth.json
4. User uploads file via Teams
5. Bot transfers file to Dev Box
6. Worker uses credential file
```

### Pros
- ✅ User does auth on familiar environment (their machine)
- ✅ No need for remote desktop

### Cons
- ⚠️ User must have Claude Code installed locally
- ⚠️ Requires secure file transfer mechanism
- ⚠️ More steps for user

### When to Use
- If Teams code exchange fails
- If user prefers to authenticate locally

---

## Option C: Kiosk Browser (RDP) ❌ Doesn't Work

### Why It Doesn't Work

With kiosk mode, user sees ONLY the browser in fullscreen. But Claude Code's flow requires:
1. Browser (for sign-in)
2. Terminal (to paste code)

User can't access both in kiosk mode!

### Verdict
**Don't use** - incompatible with copy-paste code flow

---

## Option D: Regular Browser (RDP)

### How It Works

```
1. User connects to Dev Box via browser RDP
2. User sees full desktop
3. User opens terminal
4. User runs: claude /login (or orchestrator triggers it)
5. Browser opens
6. User signs in
7. Browser shows code
8. User copies code from browser
9. User pastes code into terminal
10. Done!
```

### Pros
- ✅ User has full control
- ✅ Natural workflow
- ✅ No automation needed

### Cons
- ⚠️ User sees full desktop (not just auth)
- ⚠️ Requires RDP connection
- ⚠️ More complex than Teams flow

### When to Use
- If Teams messaging unavailable
- If user prefers direct control
- If automated flows fail

---

## Recommendation for Shraga MVP

### Primary: Option A (Teams Code Exchange)

**Use for:**
- Initial authentication
- Primary user workflow
- 95% of cases

**Implementation:**
```python
# In orchestrator.py
from claude_auth_teams import TeamsClaudeAuth

def handle_auth_required(worker_id, user_id):
    auth = TeamsClaudeAuth(send_teams_message, user_id)
    auth.request_authentication()

    # Store auth object in state
    pending_auths[user_id] = auth

def handle_teams_message(user_id, message):
    if user_id in pending_auths:
        # User sent auth code
        success = pending_auths[user_id].handle_user_code(message)
        if success:
            del pending_auths[user_id]
            # Continue with task execution
```

### Fallback: Option B (Credential File)

**Use when:**
- User unable to access Teams on their device
- Teams bot experiencing issues
- User prefers local authentication

### Emergency: Option D (Regular RDP)

**Use when:**
- All automated flows fail
- User needs to debug authentication issues
- Manual intervention required

---

## Security Comparison

| Aspect | Teams Code Exchange | Credential File | Regular RDP |
|--------|-------------------|-----------------|-------------|
| User device | Any (phone, laptop) | Must have Claude CLI | Any |
| Credentials in transit | ❌ No (just auth code) | ⚠️ Yes (credential file) | ❌ No |
| Azure authentication | ✅ Yes (for RDP if needed) | N/A | ✅ Yes |
| Can be intercepted | ⚠️ Code expires quickly | ⚠️ Token in file | ✅ RDP encrypted |
| Audit trail | ✅ Teams messages logged | ⚠️ File transfer | ✅ RDP sessions logged |

---

## Testing

### Test Teams Code Exchange

```bash
# On Dev Box
python claude_auth_teams.py

# Follow prompts:
# 1. URL will be shown
# 2. Open URL on your phone
# 3. Sign in
# 4. Enter code when prompted
```

### Test Credential File Transfer

```bash
# On local machine
claude /login
# Complete auth

# Copy credential
cp ~/.config/claude-code/auth.json auth-backup.json

# Transfer to Dev Box
# Place at: C:\Users\{user}\.config\claude-code\auth.json
```

### Test Regular RDP

```bash
# Connect to Dev Box via browser
# Open PowerShell
# Run: claude /login
# Follow prompts in terminal
```

---

## Conclusion

**For Shraga MVP, use Teams Code Exchange (Option A):**
- Simplest user experience
- Works on any device
- Secure
- No RDP needed
- Natural Teams integration

This approach perfectly aligns with:
- Users submitting tasks via Teams
- Users receiving notifications via Teams
- Users authenticating via Teams

It's the most cohesive user experience!
