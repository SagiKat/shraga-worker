# Shraga System Architecture

**Date:** February 6, 2026
**Purpose:** Architecture design for Shraga autonomous task execution system
**Status:** Planning Phase - MVP Design

---

## System Overview

Shraga is an autonomous task execution system that allows Copilot Studio team members (especially designers) to submit development tasks via Teams and receive working code executed by Claude Code on Azure Dev Boxes.

### Key Design Principles

1. **User tasks can be deleted** - Users can clean up their task list
2. **Admin always has execution history** - Mirror pattern ensures permanent audit trail
3. **Admin copy = source of truth** - Orchestrator and workers use admin copies
4. **Anonymous execution available** - Shared worker pool for users without Dev Boxes
5. **Dedicated workers on demand** - Auto-provision Dev Boxes when usage increases

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         MICROSOFT TEAMS                          │
│  ┌──────────────┐              ┌──────────────┐                 │
│  │   Designer   │              │    Admin     │                 │
│  │  (End User)  │              │   (Shraga)   │                 │
│  └──────┬───────┘              └──────┬───────┘                 │
│         │                              │                         │
└─────────┼──────────────────────────────┼─────────────────────────┘
          │                              │
          ↓                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                    COPILOT STUDIO (Shraga Agent)                 │
│  • Collects task: description, contact rules, success criteria  │
│  • Writes to Dataverse Tasks table (user-owned)                 │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ↓
┌─────────────────────────────────────────────────────────────────┐
│                          DATAVERSE                               │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│  │  Tasks   │  │  Events  │  │ Workers  │  │  Users   │       │
│  │  Table   │  │  Table   │  │  Table   │  │  Table   │       │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘       │
└────────┬────────────────────────────────────────────────────────┘
         │
         ↓ (polls every 10s)
┌─────────────────────────────────────────────────────────────────┐
│              ORCHESTRATOR (Admin's Dev Box)                      │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  • Discovers user tasks (pending, not mirrored)           │  │
│  │  • Creates admin mirror (source of truth)                 │  │
│  │  • Assigns to worker (shared pool or user's Dev Box)      │  │
│  │  • Provisions new Dev Boxes when needed                   │  │
│  │  • Monitors worker heartbeats                             │  │
│  │  • Collects progress and updates Dataverse                │  │
│  └───────────────────────────────────────────────────────────┘  │
└────────┬────────────────────────────────────────────────────────┘
         │
         ↓ (assigns work)
┌────────┴─────────────┬─────────────────┬─────────────────┐
│                      │                 │                 │
│  ┌─────────────┐     │  ┌────────┐     │  ┌────────┐     │
│  │   SHARED    │     │  │ USER'S │     │  │ USER'S │     │
│  │   WORKER    │     │  │ DEV BOX│     │  │ DEV BOX│     │
│  │   POOL      │     │  │   #1   │     │  │   #2   │     │
│  ├─────────────┤     │  ├────────┤     │  ├────────┤     │
│  │ Worker 1    │     │  │ User A │     │  │ User B │     │
│  │ Worker 2    │     │  │ Owner  │     │  │ Owner  │     │
│  │ Worker 3    │     │  │ Dedic. │     │  │ Dedic. │     │
│  │             │     │  │        │     │  │        │     │
│  │ (Admin's    │     │  │(User's │     │  │(User's │     │
│  │  Claude)    │     │  │ Claude)│     │  │ Claude)│     │
│  └─────────────┘     │  └────────┘     │  └────────┘     │
│  Anonymous           │  Authenticated  │  Authenticated  │
│  Execution           │  Execution      │  Execution      │
└──────────────────────┴─────────────────┴─────────────────┘
         │                      │                 │
         ↓                      ↓                 ↓
     Polls Dataverse       Polls Dataverse   Polls Dataverse
     for assigned tasks    for assigned tasks for assigned tasks
```

---

## Components

### 1. Copilot Studio Agent (Shraga)

**Responsibilities:**
- Collect task requirements from user via Teams
- Validate input
- Write task to Dataverse (user-owned)
- Send confirmation to user

**Technology:** Microsoft Copilot Studio (Classic Mode with topics)

---

### 2. Dataverse

**Purpose:** Central data store and communication bus

**Tables:**
- **Tasks** - Task state and results
- **Events** - Progress messages for streaming
- **Workers** - Dev Box/VM tracking
- **Users** - User metadata and roles

**Communication Pattern:** Polling-based message queue
- Orchestrator polls for new user tasks
- Workers poll for assigned tasks
- All components write progress back to Dataverse

---

### 3. Orchestrator

**Location:** Admin's Dev Box
**Purpose:** Central coordinator for task execution

**Responsibilities:**

1. **Task Discovery & Mirroring**
   - Poll for user-created tasks (not yet mirrored)
   - Create admin-owned mirror (source of truth)
   - Link user task ↔ admin mirror

2. **Worker Assignment**
   - Check if user has dedicated Dev Box
   - If yes: assign to user's worker
   - If no: assign to shared worker pool
   - Update task with `assignedWorkerId`

3. **Provisioning Management**
   - Track user task volume
   - Trigger Dev Box provisioning when threshold met
   - Monitor provisioning status
   - Notify user when Dev Box ready

4. **Health Monitoring**
   - Monitor worker heartbeats (`lastWorkerPing`)
   - Detect stalled workers
   - Reassign or fail tasks on timeout

5. **Progress Collection**
   - Monitor Events table for agent messages
   - Sync progress to user's task copy (if exists)
   - Update execution status

**Implementation:** Python script (`orchestrator.py`)

---

### 4. Workers

**Types:**

**A. Shared Worker Pool** (Phase 1 - MVP)
- Owner: Admin
- Claude Code account: Admin's account
- Purpose: Anonymous execution for users without Dev Boxes
- Count: 3-5 workers

**B. User Dedicated Dev Boxes** (Phase 2)
- Owner: Individual user
- Claude Code account: User's account (eventually)
- Purpose: Dedicated resources, user identity
- Count: 1+ per user (auto-provisioned)

**Responsibilities:**
1. Poll Dataverse for tasks assigned to this worker
2. Execute task using Claude Code
3. Report progress via Events table
4. Send heartbeat updates
5. Report completion/failure

**Implementation:** Python script (`worker.py`)

---

## Data Flow

### 1. Task Submission Flow

```
1. User submits task via Teams
   ↓
2. Shraga agent collects requirements
   ↓
3. Shraga writes to Dataverse Tasks table
   {
     owner: user@example.com,
     status: 1 (pending),
     isMirror: false,
     mirrorTaskId: null
   }
   ↓
4. User sees confirmation in Teams
```

### 2. Orchestrator Mirror Flow

```
1. Orchestrator polls Dataverse (every 10s)
   filter: "status eq 1 and isMirror eq false and mirrorTaskId eq null"
   ↓
2. Finds user task, creates admin mirror
   {
     owner: admin@example.com,
     status: 1 (pending),
     isMirror: true,
     mirrorOfTaskId: <user-task-id>
   }
   ↓
3. Links user task to mirror
   UPDATE user-task: mirrorTaskId = <admin-task-id>
   ↓
4. Creates event
   eventType: "TaskMirrored"
   content: "⚙️ Task received, assigning worker..."
```

### 3. Worker Assignment Flow

```
1. Orchestrator checks user's workers
   query: Workers WHERE userId = <user-id> AND status = 'ready'
   ↓
2. Decision:

   IF user has available worker:
     • Assign to user's worker
     • executionDetails: { workerId: <user-worker-id> }

   ELSE:
     • Assign to shared pool worker
     • executionDetails: { workerId: <shared-worker-id> }

     IF should_provision(user):
       • Trigger Dev Box provisioning
   ↓
3. Update admin mirror
   {
     assignedWorkerId: <worker-id>,
     workerStatus: 'assigned',
     status: 5 (running)
   }
   ↓
4. Create event
   eventType: "TaskAssigned"
   content: "⚙️ Task assigned to worker"
```

### 4. Worker Execution Flow

```
1. Worker polls Dataverse (every 10s)
   filter: "assignedWorkerId eq MY_ID and workerStatus eq 'assigned'"
   ↓
2. Worker picks up task
   UPDATE task: workerStatus = 'executing'
   ↓
3. Worker executes with Claude Code
   • Worker/Verifier/Summarizer loop
   • Streams progress messages
   ↓
4. Worker reports progress
   CREATE event {
     eventType: 'AgentMessage',
     content: '💭 Reading code...',
     taskId: <admin-task-id>
   }
   ↓
5. Worker sends heartbeat (every 30s)
   UPDATE task: lastWorkerPing = now()
   ↓
6. Worker completes
   UPDATE task {
     status: 7 (completed),
     workerStatus: 'done',
     output: { summary: '...', deliverables: [...] }
   }
   ↓
7. Create completion event
   eventType: 'TaskCompleted'
   content: '✅ Task completed!'
```

### 5. Progress Streaming Flow

```
1. Worker creates Events
   ↓
2. Dataverse stores events
   ↓
3. Power Automate flow triggered (on new event)
   ↓
4. Flow bot sends to Teams
   ↓
5. User sees progress in real-time
```

---

## Mirror Pattern

### Purpose

**Problem:** User can delete their task anytime, but admin needs permanent execution history.

**Solution:** Admin creates and owns a mirror copy that serves as source of truth.

### Design

**Two copies of each task:**

1. **User's Task** (informational)
   - Owner: User
   - User can read, update, **delete**
   - Receives status updates (synced from admin copy)
   - Optional - can be deleted without affecting execution

2. **Admin's Mirror** (source of truth)
   - Owner: Admin
   - User **cannot** see or modify
   - Worker operates on this copy
   - Permanent - never deleted by user
   - Contains full execution details

### Linking

```json
{
  "userTask": {
    "id": "user-123",
    "owner": "user@example.com",
    "isMirror": false,
    "mirrorTaskId": "admin-456"  // → points to admin copy
  },
  "adminMirror": {
    "id": "admin-456",
    "owner": "admin@example.com",
    "isMirror": true,
    "mirrorOfTaskId": "user-123"  // → points back to user
  }
}
```

### Sync Behavior

**During execution:**
- Admin copy updated by worker (source of truth)
- User copy optionally synced for progress visibility
- If user deletes their copy: execution continues unaffected

**After completion:**
- Admin copy remains permanently
- User copy shows final results (if not deleted)

---

## Communication Pattern

### Dataverse as Message Queue

**Why:** Simple, no new infrastructure, works across networks

**Pattern:** Polling-based

### Orchestrator → Worker

```python
# Orchestrator assigns task
update_task(admin_task_id,
    assignedWorkerId=worker.id,
    workerStatus='assigned',
    status=5  # running
)

# Worker polls for work
tasks = get_tasks(
    filter="assignedWorkerId eq MY_ID and workerStatus eq 'assigned'"
)
```

### Worker → Orchestrator

```python
# Worker reports progress
create_event(
    taskId=task.id,
    eventType='AgentMessage',
    content='💭 Reading code...'
)

# Worker updates status
update_task(task.id,
    workerStatus='executing',
    lastWorkerPing=now()
)
```

### Polling Frequencies

- **Orchestrator:** Every 10 seconds
- **Workers:** Every 10 seconds
- **Worker heartbeat:** Every 30 seconds during execution

---

## Worker Pool Strategy

### Phase 1: Shared Pool (MVP)

**Setup:**
- 3-5 Dev Boxes owned by admin
- All use admin's Claude Code account
- Anonymous execution (no user authentication)

**Pros:**
- ✅ Simple setup
- ✅ Works immediately
- ✅ No user auth required

**Cons:**
- ⚠️ All tasks run as admin
- ⚠️ Limited capacity

### Phase 2: User Dedicated (Scale)

**Trigger provisioning when:**
- User submits 5+ tasks
- User is active (not one-off)
- Shared pool consistently busy

**Provisioning flow:**
1. Orchestrator detects threshold
2. Creates Worker record (status='provisioning')
3. Calls Azure Dev Box API
4. Monitors provisioning (5-10 min)
5. Updates Worker record (status='ready')
6. Notifies user via Events

**Benefits:**
- ✅ Dedicated resources
- ✅ User identity (eventually)
- ✅ Better isolation
- ✅ Scales per user

---

## Security Model

### Row-Level Security

**Shraga User Role:**
- Tasks: **User** level (own tasks only)
- Events: **User** level (own events only)
- Workers: **User** level (own workers only)
- Users: **User** level (own record only)

**Shraga Admin Role:**
- Tasks: **Organization** level (all tasks, including mirrors)
- Events: **Organization** level (all events)
- Workers: **Organization** level (all workers)
- Users: **Organization** level (all users)

### Isolation

**User cannot:**
- ❌ See admin's mirror tasks
- ❌ Modify admin's execution records
- ❌ See other users' tasks
- ❌ Access other users' workers

**Admin can:**
- ✅ See all tasks (user + admin mirrors)
- ✅ Query execution history
- ✅ Manage all workers
- ✅ View all user activity

---

## Task Lifecycle

```
┌─────────────┐
│  1. PENDING │  User submits, Shraga writes to Dataverse
└──────┬──────┘
       │
       ↓ Orchestrator discovers
┌─────────────┐
│ 2. MIRRORED │  Admin mirror created, linked
└──────┬──────┘
       │
       ↓ Orchestrator assigns
┌─────────────┐
│ 3. ASSIGNED │  Worker ID set, status=running
└──────┬──────┘
       │
       ↓ Worker picks up
┌─────────────┐
│ 4. EXECUTING│  Worker runs Claude Code, streams progress
└──────┬──────┘
       │
       ↓ Worker completes
┌─────────────┐
│ 5. COMPLETED│  Results written, user notified
└─────────────┘

OR
       │
       ↓ Worker fails
┌─────────────┐
│  6. FAILED  │  Error details written, user notified
└─────────────┘
```

---

## Deployment

### Orchestrator

**Location:** Admin's Dev Box
**Runs:** Continuously as background process

```bash
# Set environment variables
source config.sh

# Run orchestrator
python orchestrator.py
```

### Workers

**Shared Workers** (Admin's Dev Boxes):
```bash
# Set worker ID and credentials
export WORKER_ID=<worker-guid>
source config.sh

# Run worker
python worker.py
```

**User Workers** (Auto-provisioned Dev Boxes):
- Pre-configured during provisioning
- Auto-starts on Dev Box startup
- Uses user's credentials (eventually)

### Configuration

**config.sh** (not committed):
```bash
export DATAVERSE_URL="https://....crm.dynamics.com"
export WEBHOOK_URL="<power-automate-url>"
export WEBHOOK_USER="user@example.com"
export WORKER_ID="<worker-guid>"  # For workers only
```

---

## Monitoring & Health Checks

### Orchestrator Monitoring

**Health checks:**
1. Worker heartbeats (lastWorkerPing)
   - Alert if stale > 5 minutes
   - Reassign or fail task

2. Provisioning status
   - Monitor Workers table
   - Alert if stuck > 15 minutes

3. Task queue depth
   - Count pending mirrored tasks
   - Scale shared pool if needed

### Worker Monitoring

**Health checks:**
1. Dataverse connectivity
   - Fail gracefully if can't reach
   - Retry with backoff

2. Claude Code health
   - Detect auth expiry
   - Alert admin

3. Disk space
   - Check execution folder space
   - Clean up old artifacts

---

## Future Enhancements

### Phase 3: Advanced Features

1. **Dev Server Tunnels**
   - Expose running apps via URL
   - Include tunnel URL in summary

2. **Claude Code User Auth**
   - User-specific Claude accounts
   - Device code flow via Teams

3. **Service Bus Migration**
   - Replace Dataverse polling with real-time messaging
   - Reduce latency and API calls

4. **Classic Mode Shraga**
   - Topic-based flow
   - Better error handling

5. **Evaluation Set**
   - Automated testing
   - Quality metrics

---

## Change Log

- **2026-02-06**: Initial architecture design
  - Orchestrator pattern
  - Mirror pattern (admin copy = source of truth)
  - Worker pool strategy
  - Dataverse communication
  - Security model
