# CoworkOps AI — Requirements

## Introduction

CoworkOps AI is a single-user assistant for a coworking space manager/owner. The manager pastes messy, unstructured text (WhatsApp messages, notes, quick texts) describing a room booking, a payment, or an event. A small open-weight LLM (Qwen3-4B via Ollama, max 8B parameters) extracts structured data from the text; the manager reviews/edits a preview and saves it to a local SQLite database. The app provides a month-wise dashboard (bookings, revenue, room utilization, events) and a natural-language "Ask AI" interface that answers questions from the stored data.

The product is an MVP intended to be built in under 90 minutes using Python, Streamlit, and SQLite, and must be demonstrable end to end. The open-weight model is the core capability: without it, intake and Q&A do not function.

### Definitions

- **Record type:** one of `booking`, `payment`, or `event`.
- **Payment status:** one of `paid`, `pending`.
- **Amount:** a monetary value in Pakistani Rupees (PKR).
- **Room utilization:** total booked hours per room within the selected month.
- **Model:** the configured open-weight LLM served locally by Ollama (default `qwen3:4b`).
- **Target month:** the year-and-month selected by the manager on the dashboard.

### Technology & Operating Constraints

- Language/stack: Python, Streamlit, SQLite.
- AI: open-weight model (≤ 8B parameters) run locally via Ollama; model name configurable in a `.env` file.
- No secrets committed to the repository.
- Public GitHub repository with an MIT license.
- Single user; no authentication/login.
- Scope kept small to allow build in under 90 minutes.
- Seed/demo data covering September 2026 and October 2026.

---

## Requirements

### Requirement 1: Smart Intake — Extraction

**User Story:** As a coworking space manager, I want to paste a messy message and have the AI extract structured fields, so that I don't have to manually re-type data from WhatsApp or notes.

#### Acceptance Criteria

1. WHEN the manager submits pasted text for intake THEN the system SHALL send the text to the configured open-weight model via Ollama and request a structured extraction.
2. WHEN the model returns an extraction THEN the system SHALL parse the following fields: record type, customer/organizer name, room, date, start time, end time, amount (PKR), payment status, and event name.
3. WHERE a field is not present or cannot be determined from the input text THEN the system SHALL leave that field empty (or null) rather than fabricating a value.
4. WHEN the model classifies the record type THEN the system SHALL constrain the value to one of `booking`, `payment`, or `event`.
5. IF the model is unreachable, returns an error, or returns output that cannot be parsed into the expected fields THEN the system SHALL display an error message to the manager and SHALL NOT save any record.
6. IF the submitted intake text is empty THEN the system SHALL NOT call the model and SHALL prompt the manager to enter text.

### Requirement 2: Smart Intake — Preview, Edit, and Save

**User Story:** As a coworking space manager, I want to review and correct the extracted fields before saving, so that only accurate data enters the database.

#### Acceptance Criteria

1. WHEN the system completes a successful extraction THEN the system SHALL display all extracted fields in an editable preview form.
2. WHEN the manager edits any field in the preview THEN the system SHALL retain the edited value for saving.
3. WHEN the manager confirms the preview THEN the system SHALL validate that record type is one of `booking`, `payment`, or `event`, and that payment status (when present) is one of `paid` or `pending`.
4. IF validation fails on confirmation THEN the system SHALL display which field is invalid and SHALL NOT save the record.
5. WHEN the manager confirms a valid preview THEN the system SHALL persist the record to the SQLite database and SHALL display a success confirmation.
6. WHEN the manager discards or cancels a preview THEN the system SHALL NOT persist any record.

### Requirement 3: Rooms

**User Story:** As a coworking space manager, I want a list of rooms with their capacity and hourly rate, so that bookings and utilization are tied to real rooms.

#### Acceptance Criteria

1. WHEN the database is first initialized THEN the system SHALL seed exactly 5 rooms, each with a name, a capacity, and an hourly rate.
2. WHEN the manager opens the Rooms view THEN the system SHALL display each room's name, capacity, and hourly rate.
3. WHERE an intake record references a room THEN the system SHALL associate the record with a seeded room by name.

### Requirement 4: Month-wise Dashboard

**User Story:** As a coworking space manager, I want a month-wise dashboard, so that I can see bookings, revenue, utilization, and events for a chosen month at a glance.

#### Acceptance Criteria

1. WHEN the manager opens the dashboard THEN the system SHALL provide a month selector for choosing the target month.
2. WHEN a target month is selected THEN the system SHALL display the total number of bookings for that month.
3. WHEN a target month is selected THEN the system SHALL display the total revenue (sum of amounts of records marked `paid`) in PKR for that month.
4. WHEN a target month is selected THEN the system SHALL display the total pending payments (sum of amounts of records marked `pending`) in PKR for that month.
5. WHEN a target month is selected THEN the system SHALL display room utilization per room, computed as total booked hours per room within that month.
6. WHEN a target month is selected THEN the system SHALL display upcoming events and events grouped by organizer for that month.
7. WHERE no records exist for the selected month THEN the system SHALL display zeroed/empty metrics without error.

### Requirement 5: Ask AI (Natural-Language Q&A)

**User Story:** As a coworking space manager, I want to ask questions in plain English, so that I can get answers from my data without writing queries.

#### Acceptance Criteria

1. WHEN the manager submits a natural-language question THEN the system SHALL use the configured open-weight model together with the stored data to produce an answer.
2. WHEN the system answers a question THEN the system SHALL base the answer on data retrieved from the SQLite database (e.g., "Which room was used least in October?").
3. IF the model is unreachable or returns an error THEN the system SHALL display an error message instead of an answer.
4. IF the submitted question is empty THEN the system SHALL NOT call the model and SHALL prompt the manager to enter a question.

### Requirement 6: Local Open-Weight Model Configuration

**User Story:** As a coworking space manager, I want the AI to run locally with a configurable model, so that I control the model and keep data on my machine.

#### Acceptance Criteria

1. WHERE the app requires the model name THEN the system SHALL read it from a `.env` file, defaulting to `qwen3:4b` when unset.
2. WHEN the system invokes the model THEN the system SHALL call it locally through Ollama.
3. THE system SHALL NOT require a model larger than 8B parameters for any AI feature.
4. THE repository SHALL NOT contain any secrets or credentials; configuration SHALL be provided via `.env` which is excluded from version control.

### Requirement 7: Data Persistence and Seed/Demo Data

**User Story:** As a coworking space manager, I want my data stored locally and some demo data available, so that the app is usable and demonstrable immediately.

#### Acceptance Criteria

1. WHEN a record is saved THEN the system SHALL persist it to a local SQLite database file.
2. WHEN the database is first initialized THEN the system SHALL seed demo data for September 2026 and October 2026 covering bookings, payments, and events across the 5 seeded rooms.
3. WHEN the app restarts THEN the system SHALL retain all previously saved records.

### Requirement 8: Repository and Licensing

**User Story:** As the project owner, I want a clean public repository, so that the project can be shared and reused.

#### Acceptance Criteria

1. THE repository SHALL be publishable as a public GitHub repository.
2. THE repository SHALL include an MIT license.
3. THE repository SHALL include setup/run instructions sufficient to demonstrate the app end to end (intake → preview → save → dashboard → Ask AI).

---

## Out of Scope

The following are explicitly **not** part of this MVP:

- Authentication, login, user accounts, or multi-user support.
- Role-based access or permissions.
- Multiple coworking locations/branches or multi-tenancy.
- Editing or deleting records after save, and full CRUD management of rooms (add/remove/update rooms via UI).
- Calendar integrations, email/WhatsApp integrations, or automatic message ingestion (manager pastes text manually).
- Online payment processing, invoicing, receipts, or payment gateways.
- Currencies other than PKR, or currency conversion.
- Cloud/hosted model APIs, remote inference, or models larger than 8B parameters.
- Automated conflict detection / double-booking prevention and room availability scheduling.
- Notifications, reminders, or alerts.
- Export to CSV/PDF, reporting beyond the month-wise dashboard, and charts beyond basic month metrics.
- Backups, data migration tooling, and audit logging.
- Mobile apps or responsive-design guarantees beyond default Streamlit behavior.
- Internationalization/localization of the UI.
- Automated test suite as a delivery requirement (not required unless requested).
