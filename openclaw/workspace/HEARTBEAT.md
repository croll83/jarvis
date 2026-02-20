# HEARTBEAT.md

## Routing-Aware Heartbeat

Before executing heartbeat tasks, classify and route:

### Step 1: Classify heartbeat complexity
Run: `node skills/intelligent-router/intelligent-router-hook.js "heartbeat: check emails, calendar, notifications"`
- SIMPLE tasks (email check, calendar, status) → handle directly with current model (Haiku)
- MEDIUM+ tasks → spawn sub-agent with routed model

### Step 2: Log routing decision
Run: `node routing-logger.mjs log --source heartbeat --agent <AGENT_ID> --job "Heartbeat Check" --task "<description>" --tier <TIER> --model <MODEL>`

Agent IDs: `main`, `family:ada`, `family:giorgio`, `family:sofia`, `personal`

### Step 3: Execute checks
Standard heartbeat checks (rotate 2-4x/day):
- [ ] Email — urgent unread?
- [ ] Calendar — events in next 24-48h?
- [ ] Twitter mentions / notifications
- [ ] Weather (if relevant)

### Step 4: Sub-agent for complex tasks
If heartbeat discovers something that needs MEDIUM+ work:
```
node skills/intelligent-router/spawn-with-routing.js "detailed task description here"
```

### Rules
- Most heartbeat checks are SIMPLE → Haiku handles directly, no spawn needed
- Only spawn sub-agent if a discovered task requires deeper analysis
- Always log the routing decision for visibility
- Check `memory/heartbeat-state.json` for last check times
