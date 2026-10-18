# ResourceHub

<p align="center">
  <strong>One workspace for your company's resources, inventory, staff, and lending operations.</strong>
</p>

ResourceHub is a web-based resource and inventory management system designed to help organizations keep track of the equipment and items they own, the people who use them, and the movement of those items over time. Instead of relying on scattered spreadsheets, handwritten records, or disconnected tools, ResourceHub brings day-to-day inventory operations into one place.

Built with Python and SQLite, ResourceHub is being developed with a practical goal: make it easier for companies and teams to know what they have, who is responsible for it, what has been borrowed or returned, and how their operations are performing.

> **Project status:** ResourceHub is actively being developed. This README separates functionality already present in the project from ideas on the roadmap. Planned capabilities are not presented as completed features.

---

## Contents

- [Why ResourceHub?](#why-resourcehub)
- [What ResourceHub does today](#what-resourcehub-does-today)
- [How it works](#how-it-works)
- [Who it is for](#who-it-is-for)
- [Technology](#technology)
- [Getting started](#getting-started)
- [Configuration and deployment](#configuration-and-deployment)
- [Security and data protection](#security-and-data-protection)
- [Testing](#testing)
- [Roadmap: what ResourceHub can become](#roadmap-what-resourcehub-can-become)
- [Contributing](#contributing)

## Why ResourceHub?

Every organization depends on resources: laptops, tools, devices, equipment, supplies, and other assets that people need to do their jobs. When records are incomplete or spread across different places, teams can lose track of stock, miss returns, struggle to identify who has an item, and find it difficult to produce reliable reports.

ResourceHub aims to solve that operational problem with a clear, centralized workflow.

- **Know what you own.** Keep inventory records in one place.
- **Know who is responsible.** Associate staff and resource activity with the people using the system.
- **Track movement.** Record borrowing and returns rather than relying on memory.
- **Understand activity.** Use reports to review stock, borrowing, and overdue items.
- **Give each company its own space.** The web application supports separate shop/company accounts and their operational records.
- **Make the workspace yours.** Owners can configure available shop identity and appearance settings.

## What ResourceHub does today

The following describes functionality present in the current project. Exact options may vary as development continues.

### Inventory and resource tracking

- Maintain resource records with identifiers, names, and quantities.
- Track available stock and quantities currently in use.
- Keep resource information in a SQLite database.
- Support inventory operations through the web interface and the project's command-line program.

### Borrowing and returns

- Record resource loans and returns.
- Associate lending activity with the staff member or borrower recorded by the system.
- Apply borrowing limits in the existing lending workflow.
- Calculate due dates and identify overdue items in reports.
- Protect stock counts from rejected borrowing and invalid over-return operations.

### Staff and company workspaces

- Allow companies to register separate accounts in the multi-shop web application.
- Provide an owner role for company-level management and a staff role for day-to-day operations.
- Let owners manage staff for their company.
- Keep registered companies' operational data separated into their own shop data stores.

### Sales and operational records

The web application includes workflows for recording sales alongside inventory, loans, and returns. These records support a more complete picture of how items are moving through a company's day-to-day operations.

### Reports and backups

- Review inventory and lending information, including overdue items.
- Use reporting functionality to understand resource activity.
- Export supported reports to CSV from the command-line application.
- Create database snapshots and daily backups in the web application.
- Download a ZIP backup from the owner's Reports area.

Backups are a recovery aid, not a substitute for an independent copy. Keep private backups somewhere separate from the server.

### Company settings and email

- Configure supported company identity and appearance settings, including shop name, brand color, currency, and logo.
- Use the server's private environment configuration for SMTP email delivery.
- Send account-verification email as part of the registration workflow when email is configured.
- Use the owner-only test-email action to check whether the configured email service can send a message.

SMTP credentials belong on the server, never in browser code or committed files.

## How it works

A typical ResourceHub workflow looks like this:

1. **Set up a company workspace.** A company registers and its owner manages the workspace.
2. **Add resources.** Record the equipment or items the company needs to track.
3. **Manage access.** The owner manages staff who will use the workspace.
4. **Record activity.** Staff use the available workflows to record sales, loans, and returns.
5. **Review the records.** Use reports to understand available stock, lending activity, and overdue resources.
6. **Protect the data.** Maintain backups and configure persistent storage for deployments.

The goal is to make resource accountability part of normal work—not a separate administrative burden.

## Who it is for

ResourceHub is intended for teams that need a straightforward way to manage physical resources and the people using them, including:

- Small and medium-sized businesses
- Offices and administrative teams
- Schools, labs, and training centers
- Equipment lending desks
- Teams that share laptops, tools, devices, or other assets
- Organizations that need company-specific inventory and staff workflows

These are target use cases, not a claim that every industry-specific workflow is already implemented.

## Technology

| Area | Current technology |
|---|---|
| Application language | Python |
| Web application | Python standard-library HTTP server in `web_app.py` |
| Data storage | SQLite |
| Command-line program | `main.py` |
| Configuration | Environment variables and a private local `.env` file |
| Testing | Python `unittest` |

The repository also contains a separate Flask starter application (`app.py`). The Flask starter and the ResourceHub web application in `web_app.py` are different entry points; follow the instructions for the application you intend to run.

## Getting started

### Requirements

- Python 3.10 or newer is recommended; use the Python version supported by your environment.
- Git, if you are cloning the repository.
- A writable local directory for the database and application data.

### 1. Get the project

```bash
git clone https://github.com/kristopher1027/management-system.git
cd management-system
```

If you already have the project locally, move into its directory instead.

### 2. Run the ResourceHub web application

```bash
python3 web_app.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

Run the server from the project directory for the default local setup. Stop it with `Ctrl+C`.

### 3. Configure email for registration

The application reads SMTP settings from a private `.env` file in the configured data directory. For the default local setup, that is the project directory.

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` and replace the example values:

```dotenv
RESOURCEHUB_SMTP_HOST=smtp.gmail.com
RESOURCEHUB_SMTP_PORT=587
RESOURCEHUB_SMTP_FROM=your-sending-address@gmail.com
RESOURCEHUB_SMTP_USERNAME=your-sending-address@gmail.com
RESOURCEHUB_SMTP_PASSWORD=your-16-character-google-app-password
```

For Gmail, use a Google **App Password**, not your normal Google account password. Never paste real credentials into this README, source code, screenshots, issues, or commits. The `.env` file should remain ignored by Git; `.env.example` is the safe template.

For public registration, configure your own Cloudflare Turnstile site and secret keys according to the application's environment configuration.

### 4. Run the command-line program (optional)

The repository also includes a command-line inventory program:

```bash
python3 main.py
```

It uses SQLite and supports its own inventory and reporting workflow. Keep in mind that `main.py`, the separate Flask starter, and `web_app.py` are distinct entry points with different behavior and setup.

## Configuration and deployment

The web application supports configuration through environment variables. Common deployment settings include:

- `RESOURCEHUB_DATA_DIR` — directory for persistent application data.
- `RESOURCEHUB_HOST` — network interface to bind to; the default is local-only.
- `PORT` — port for the web application.
- `RESOURCEHUB_COOKIE_SECURE` — enable secure session cookies when serving over HTTPS.
- `RESOURCEHUB_SMTP_HOST`, `RESOURCEHUB_SMTP_PORT`, `RESOURCEHUB_SMTP_FROM`, `RESOURCEHUB_SMTP_USERNAME`, and `RESOURCEHUB_SMTP_PASSWORD` — SMTP delivery settings.

For a hosted deployment, point `RESOURCEHUB_DATA_DIR` at a **persistent disk or volume** and ensure the process can write to it. For example, on a Linux host:

```bash
export RESOURCEHUB_DATA_DIR="/path/to/persistent/volume"
export RESOURCEHUB_HOST="0.0.0.0"
export PORT="8000"
python3 web_app.py
```

Use a real persistent path provided by your hosting platform; do not copy the example path literally. Put the application behind HTTPS before making it publicly accessible, and set `RESOURCEHUB_COOKIE_SECURE=1` when the HTTPS setup supports it. Restrict access to the data directory and backups.

A deployment with an ephemeral filesystem is suitable for a short-lived demo only: records may be lost when the service restarts or is redeployed. The ZIP backup can contain sensitive company records and password hashes, so store and share it carefully.

### Flask starter is separate

The repository's `app.py` is a separate Flask starter. Its deployment and `RESOURCEHUB_SECRET_KEY` configuration do not automatically apply to `web_app.py`. If you choose to run the Flask starter, inspect its own requirements and configuration before deploying it. Do not assume deploying `app.py` deploys the ResourceHub multi-company web application.

## Security and data protection

ResourceHub includes security-focused controls such as role checks for owner-only operations, password hashing, session-cookie protections, request validation, and checks intended to prevent unauthorized cross-origin requests. Security must continue to be tested as the application evolves.

For safer operation:

- Keep `.env`, live databases, account data, and backups out of version control.
- Use unique, strong credentials and an App Password for Gmail SMTP.
- Use HTTPS for public deployments.
- Keep dependencies and the hosting environment updated.
- Restrict access to persistent data and exported backups.
- Test backup restoration, not just backup creation.
- Review authentication, authorization, session handling, and tenant isolation before handling sensitive production data.

No README or individual security feature is a guarantee that an application is completely secure. Production readiness requires ongoing testing, monitoring, and operational safeguards.

## Testing

Run the project's unit tests from the repository root:

```bash
python3 -m unittest discover -v
```

Check Python syntax after making changes:

```bash
python3 -m py_compile main.py web_app.py
```

When contributing a change, run the relevant tests and review the diff before committing:

```bash
git diff --check
git status --short
git diff
```

## Roadmap: what ResourceHub can become

The long-term vision is to grow ResourceHub from a useful inventory and lending application into a dependable resource-operations platform for organizations with multiple teams and locations. The following are **future possibilities**, not promises that these features already exist.

### 1. Stronger asset lifecycle management

- Unique asset tags and QR-code or barcode scanning
- Serial numbers, photos, categories, conditions, and locations
- Maintenance schedules, repair history, warranties, and replacement planning
- Full audit history for changes to important asset records

### 2. A complete approval and handover workflow

- Configurable loan periods and approval policies
- Authorized-officer approval before an item is issued
- Digital handover and return confirmations
- Condition checks, signatures, and clear responsibility records
- Automated reminders for due dates and overdue returns

### 3. Organization-wide operations

- Multiple branches, departments, warehouses, and storage locations
- Fine-grained roles and permissions
- Invitations and controlled onboarding for staff
- Central oversight with clear separation between company workspaces
- Organization-level reporting across authorized locations

### 4. Better insights and reporting

- Visual dashboards for stock, usage, overdue loans, and asset utilization
- Custom date ranges and downloadable reports
- Low-stock and unusual-activity alerts
- Trends that help organizations plan purchases and reduce unused inventory

### 5. More integrations and notifications

- Reliable email notifications for approvals, due dates, and account events
- Optional integrations with identity providers and business systems
- Import tools for existing spreadsheets and export tools for migration
- APIs for approved integrations with other company software

### 6. Production-grade reliability

- Automated backup verification and guided restoration
- Database migration and upgrade procedures
- Structured application logs, monitoring, and health checks
- Automated security, integration, and regression tests
- Documented deployment, recovery, and incident-response procedures

### 7. A more complete user experience

- Responsive mobile-friendly workflows for staff in the field
- Faster search, filtering, and bulk updates
- Accessibility improvements and clearer error messages
- Guided setup for new companies and administrators
- Optional offline-friendly workflows, if the architecture and data consistency requirements support them

### What “finished” should mean

A mature ResourceHub should do more than store records. It should help an organization answer, accurately and quickly:

- What assets do we have, and where are they?
- Who is responsible for each asset right now?
- What is available, borrowed, overdue, damaged, or awaiting approval?
- Which assets need maintenance or replacement?
- What changed, who changed it, and when?
- Can we recover our records if a server or database fails?

Reaching that level means validating the core workflows, tightening tenant and permission boundaries, proving that backups can be restored, testing realistic failure cases, and deploying with persistent storage and HTTPS. The roadmap should be delivered in tested, incremental releases.

## Contributing

Contributions, bug reports, and practical feature ideas are welcome. Before opening a pull request:

1. Explain the problem and the intended behavior.
2. Keep changes focused and avoid committing secrets or local data.
3. Add or update tests for changed behavior.
4. Run the test suite and `git diff --check`.
5. Document configuration changes and any migration or deployment impact.

## Project

**Repository:** [kristopher1027/management-system](https://github.com/kristopher1027/management-system)

ResourceHub is built around a simple principle: **every resource should be visible, every handover should be accountable, and every team should have a clearer picture of what it manages.**
