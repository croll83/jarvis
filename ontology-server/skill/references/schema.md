# Ontology Schema Reference v2.2
# Full type definitions, semantic relations, and constraint patterns for the autonomous agent graph.

types:
  ## 1. Core Types & Identity
  Person:
    required: [name]
    properties:
      name: string
      type_enum: [Agent, Human]
      role_enum: [admin, user]  # Admin can bypass ACL, user is standard
      gender_enum: [male, female, other]
      email: string?
      phone: string?
      date_of_birth: date?     # ISO 8601 (YYYY-MM-DD)
      timezone: string?        # IANA timezone (e.g., "Europe/Rome")
      organization: ref(Organization)?
      interaction_rules: string?  # Instructions on how the agent should talk/act with this person
      preferences: ref(Preference)[]?
      visibility_enum: [private, family, public] # For ACL/speaker_id filtering
      notes: string?
      tags: string[]?

  Organization:
    required: [name]
    properties:
      name: string
      description: string?
      type_enum: [company, team, community, government, other]
      website: url?
      members: ref(Person)[]?
      visibility_enum: [private, family, public]

  Topic:
    required: [name]
    properties:
      name: string
      parent_topic: ref(Topic)?
      description: string?
      tags: string[]?
      visibility_enum: [private, family, public]

  Preference:
    required: [subject, value]
    properties:
      subject: string       # e.g., "coffee_type", "meeting_start_time"
      value: any            # e.g., "espresso", "after 10:00"
      context: string?      # e.g., "work", "home"
      owner: ref(Person)
      visibility_enum: [private, family, public]

  ## 2. Work & Execution Management
  Project:
    required: [name]
    properties:
      name: string
      description: string?
      status_enum: [planning, active, paused, completed, archived]
      owner: ref(Person)?
      team: ref(Person)[]?
      goals: ref(Goal)[]?
      topic: ref(Topic)?
      start_date: date?
      end_date: date?
      tags: string[]?
      visibility_enum: [private, family, public]

  Task:
    required: [title, status]
    properties:
      title: string
      description: string?
      status_enum: [open, in_progress, blocked, done, cancelled]
      priority_enum: [low, medium, high, urgent]
      assignee: ref(Person|Agent)?
      project: ref(Project)?
      strategy: ref(Strategy)? # Links execution to an automated strategy
      due: datetime?
      estimate_hours: number?
      blockers: ref(Task)[]?
      tags: string[]?
      visibility_enum: [private, family, public]

  Goal:
    required: [description]
    properties:
      description: string
      target_date: date?
      status_enum: [active, achieved, abandoned]
      metrics: object[]?
      key_results: string[]?
      visibility_enum: [private, family, public]

  Strategy:
    required: [name, rules, status]
    properties:
      name: string
      description: string?
      topic: ref(Topic)?        # Semantic link (e.g., "Crypto Portfolio")
      target_assets: string[]?  # e.g., ["BTC", "ETH"]
      budget_limit: number?
      rules: object[]           # Structured conditions { "trigger": "price < 50k", "action": "buy 10%" }
      status_enum: [active, paused, backtesting]
      visibility_enum: [private, family, public]

  ## 3. Financial & On-Chain Operations
  Transaction:
    required: [type, account, timestamp, status]
    properties:
      type_enum: [buy, sell, swap, transfer, stake, yield]
      account: ref(Account)        # Wallet/Account used
      asset_in: string?            # What you gave (e.g., "USDT")
      amount_in: number?
      asset_out: string?           # What you received (e.g., "ETH")
      amount_out: number?
      fee: number?
      fee_asset: string?
      tx_hash: string?             # On-chain or bank ID
      status_enum: [pending, confirmed, failed]
      timestamp: datetime
      executor: ref(Agent|Person)  # Who pulled the trigger
      notes: string?
      visibility_enum: [private, family, public]

  ## 4. Time & Location
  Event:
    required: [title, start]
    properties:
      title: string
      description: string?
      start: datetime
      end: datetime?
      location: ref(Location)?
      attendees: ref(Person)[]?
      recurrence: object?  # iCal RRULE format
      status_enum: [confirmed, tentative, cancelled]
      reminders: object[]?
      visibility_enum: [private, family, public]

  Location:
    required: [name]
    properties:
      name: string
      description: string?
      address: string?
      city: string?
      country: string?
      coordinates: object?  # {lat, lng}
      timezone: string?
      endpoint_map: string? # For Domotica API refs
      visibility_enum: [private, family, public]

  ## 5. Information
  Document:
    required: [title]
    properties:
      title: string
      path: string?  # Local file path
      url: url?      # Remote URL
      mime_type: string?
      summary: string?
      content_hash: string?
      topic: ref(Topic)?
      tags: string[]?
      visibility_enum: [private, family, public]

  Message:
    required: [content, sender]
    properties:
      content: string
      sender: ref(Person)
      recipients: ref(Person)[]
      thread: ref(Thread)?
      timestamp: datetime
      platform: string?  # email, slack, whatsapp, etc.
      external_id: string?
      visibility_enum: [private, family, public]

  Thread:
    required: [subject]
    properties:
      subject: string
      participants: ref(Person)[]
      messages: ref(Message)[]
      status_enum: [active, archived]
      last_activity: datetime?
      visibility_enum: [private, family, public]

  Note:
    required: [content]
    properties:
      content: string
      title: string?
      tags: string[]?
      refs: ref(Entity)[]?  # Links to any entity
      topic: ref(Topic)?
      created: datetime
      visibility_enum: [private, family, public]

  ## 6. Resources & Skills
  Account:
    required: [service, username]
    properties:
      service: string  # github, debank, binance, aws, gog, google, etc.
      type_enum: [crypto_wallet, bank, software, platform, other]
      username: string
      url: url?
      authorizations: string[]?  # Scopes granted (e.g., ["email", "drive", "contacts", "calendar"])
      credential_ref: ref(Credential)?
      skill: ref(Skill)? # How the agent interacts with it
      visibility_enum: [private, family, public]

  Skill:
    required: [name]
    properties:
      name: string                 # e.g., "debank_api", "aws_cli", "gogcli"
      description: string?
      binary: string?              # Local executable path (e.g., "/usr/local/bin/gogcli")
      skill_md: string?            # Path to SKILL.md for AI Agent integration
      parameters: object?          # e.g., {"address": "ref(Account.address)", "chain": "string"}
      output_format: string?       # e.g., "json"
      visibility_enum: [private, family, public]

  Device:
    required: [name, type]
    properties:
      name: string
      description: string?
      type_enum: [computer, phone, tablet, server, iot, other]
      os: string?
      identifiers: object?  # {mac, serial, etc.}
      owner: ref(Person)?
      visibility_enum: [private, family, public]

  Credential:
    required: [service, secret_ref]
    forbidden_properties: [password, secret, token, key, api_key]
    properties:
      service: string
      secret_ref: string  # Reference to secret store (e.g., "keychain:github-token")
      expires: datetime?
      scope: string[]?
      visibility_enum: [private, family, public]

  ## 7. Meta
  Action:
    required: [type, target, timestamp]
    properties:
      type: string  # create, update, delete, send, etc.
      target: ref(Entity)
      timestamp: datetime
      actor: ref(Person|Agent)?
      outcome_enum: [success, failure, pending]
      details: object?
      visibility_enum: [private, family, public]

  Policy:
    required: [scope, rule]
    properties:
      scope: string  # What this policy applies to
      rule: string   # The constraint in natural language or code
      enforcement_enum: [block, warn, log]
      enabled: boolean
      visibility_enum: [private, family, public]

relations:
  ## 8. Relation Types (The "Verbs")
  owns:
    from_types: [Person, Organization]
    to_types: [Account, Device, Document, Project, Location, Organization]
    cardinality: one_to_many

  has_owner:
    from_types: [Project, Task, Document, Topic, Organization, Location]
    to_types: [Person]
    cardinality: many_to_one

  related_to:
    from_types: [Person]
    to_types: [Person]
    cardinality: many_to_many
    description: "Family, social, or professional relationship between two people. Use relation_type property to specify. Only store direct relations — transitive ones (grandparent, uncle, etc.) are inferred by POST /relations/infer."
    properties:
      relation_type_enum: [spouse, parent, child, sibling, friend, colleague, acquaintance, employer, employee]

  assigned_to:
    from_types: [Task]
    to_types: [Person, Agent]
    cardinality: many_to_one

  has_task:
    from_types: [Project, Strategy]
    to_types: [Task]
    cardinality: one_to_many

  has_goal:
    from_types: [Project, Strategy]
    to_types: [Goal]
    cardinality: one_to_many

  member_of:
    from_types: [Person]
    to_types: [Organization]
    cardinality: many_to_many

  part_of:
    from_types: [Task, Document, Event, Account]
    to_types: [Project, Topic]
    cardinality: many_to_one

  part_of_thread:
    from_types: [Message]
    to_types: [Thread]
    cardinality: many_to_one

  blocks:
    from_types: [Task]
    to_types: [Task]
    acyclic: true
    cardinality: many_to_many

  depends_on:
    from_types: [Task, Project]
    to_types: [Task, Project, Event]
    acyclic: true
    cardinality: many_to_many

  requires:
    from_types: [Action, Skill]
    to_types: [Credential, Policy]
    cardinality: many_to_many

  supersedes:
    from_types: [Document, Strategy]
    to_types: [Document, Strategy]
    cardinality: one_to_one
    description: "Marks the target entity as obsolete, replacing it with the source entity"

  mentions:
    from_types: [Document, Message, Note]
    to_types: [Person, Project, Task, Event, Topic]
    cardinality: many_to_many

  references:
    from_types: [Document, Note]
    to_types: [Document, Note, Topic]
    cardinality: many_to_many

  follows_up:
    from_types: [Task, Event]
    to_types: [Event, Message]
    cardinality: many_to_one

  is_expert_in:
    from_types: [Person, Agent]
    to_types: [Topic, Skill]
    cardinality: many_to_many
    properties:
      level_enum: [beginner, intermediate, expert]

  has_preference:
    from_types: [Person]
    to_types: [Preference]
    cardinality: one_to_many
    description: "For operational preferences (key-value config): coffee_type=espresso, meeting_start=after 10:00. For interests/hobbies, use interested_in → Topic instead."

  interested_in:
    from_types: [Person]
    to_types: [Topic]
    cardinality: many_to_many
    description: "Links a person to their interests, hobbies, passions. Use Topic hierarchy (parent_topic) for categories: Sport > Calcio > SSC Napoli."
    properties:
      intensity_enum: [casual, moderate, passionate]

  represents:
    from_types: [Agent]
    to_types: [Person]
    cardinality: many_to_one
    description: "Indicates if the AI agent is acting on behalf of a specific human"

  delegates_to:
    from_types: [Person]
    to_types: [Person, Agent]
    cardinality: many_to_many
    description: "Used to shift execution responsibility without losing ownership"

  executed_by:
    from_types: [Transaction]
    to_types: [Person, Agent]
    cardinality: many_to_one

  originated_from:
    from_types: [Transaction]
    to_types: [Task, Strategy]
    cardinality: many_to_one
    description: "Links the financial execution to the intent (Task) or the automated Strategy"

  affects_account:
    from_types: [Transaction]
    to_types: [Account]
    cardinality: many_to_one

  has_credential:
    from_types: [Account]
    to_types: [Credential]
    cardinality: one_to_many
    description: "Links an account to the credentials used to authenticate with it"

  attendee_of:
    from_types: [Person]
    to_types: [Event]
    cardinality: many_to_many
    properties:
      status_enum: [accepted, declined, tentative, pending]

  located_at:
    from_types: [Event, Person, Device]
    to_types: [Location]
    cardinality: many_to_one

  has_skill:
    from_types: [Location, Person, Organization]
    to_types: [Skill]
    cardinality: many_to_many
    description: "Links a location, person, or organization to skills/capabilities available there"

## 9. Global Constraints
constraints:
  - type: Credential
    rule: "forbidden_properties: [password, secret, token]"
    message: "Credentials must use secret_ref to reference external secret storage"

  - type: Task
    rule: "status transitions: open -> in_progress -> (done|blocked|cancelled) -> done"
    enforcement: warn

  - type: Event
    rule: "if end exists: end >= start"
    message: "Event end time must be after start time"

  - type: Task
    rule: "has_relation(part_of, Project) OR has_property(assignee) OR has_relation(part_of, Strategy)"
    enforcement: warn
    message: "Task should belong to a project, strategy, or have an explicit assignee"

  - relation: blocks
    rule: "acyclic"
    message: "Circular task dependencies are not allowed"

  - type: Transaction
    rule: "if status == confirmed, entity becomes read-only"
    enforcement: block
    message: "Confirmed financial transactions cannot be altered, only annotated via Notes"
