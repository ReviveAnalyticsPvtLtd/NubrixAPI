# NubrixAI Analytics Hub API — Endpoints Specification

All endpoints are mounted under `/api/latest/`. Authentication is via `Authorization: Bearer <jwt_token>` header (HS256 signed with `SECRET_KEY`).

## Table of Contents
1. [Authentication](#1-authentication)
2. [Project Management](#2-project-management)
3. [Data Loading](#3-data-loading)
4. [Table Viewer](#4-table-viewer)
5. [Metadata & Insights](#5-metadata--insights)
6. [Reporting & Charts](#6-reporting--charts)
7. [Data Blends](#7-data-blends)
8. [Dashboard](#8-dashboard)
9. [Transformations](#9-transformations)
10. [Utility Endpoints](#10-utility-endpoints)
11. [Subscriptions](#11-subscriptions)
12. [Billing Admin](#12-billing-admin)
13. [Webhooks](#13-webhooks)
14. [Credits](#14-credits)
15. [Admin Panel](#15-admin-panel)

---

## 1. Authentication

All auth routes are under `/api/latest/auth/`.

### 1.1 `GET /api/latest/auth/verify`

Verify a JWT token and return the user's credit balance snapshot.

**Auth:** Required (`verifyUser`)

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Token is valid and has not expired.",
  "credits": {
    "planTier": "pro",
    "monthlyTokenQuota": 10000000,
    "usedTokens": 480000,
    "remainingTokens": 9520000,
    "monthlyCredits": 1000.0,
    "usedCredits": 48.0,
    "remainingCredits": 952.0,
    "usagePercentage": 4.8,
    "periodStart": "2026-07-01T00:00:00Z",
    "periodEnd": "2026-08-01T00:00:00Z",
    "lastResetAt": "2026-07-01T00:00:00Z",
    "initialized": true
  }
}
```

---

### 1.2 `POST /api/latest/auth/signUp`

Register a new user.

**Auth:** Not required

**Request Body:**
```json
{
  "email": "user@example.com",
  "password": "securePassword123"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "userId": "62b43abe-f5b6-48ea-8231-35ee822bff71",
  "profileImage": "https://rnyvgjoacnpvscanmhnj.supabase.co/.../default-avatar.png"
}
```

---

### 1.3 `GET /api/latest/auth/confirmMail/{userId}`

Resend the confirmation email to a user.

**Path Params:** `userId` (str)

**Response (200):**
```json
{
  "status": "SUCCESS"
}
```

---

### 1.4 `POST /api/latest/auth/login`

Authenticate with email + password.

**Request Body:**
```json
{
  "email": "user@example.com",
  "password": "securePassword123"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "token": "eyJ...",
  "userId": "62b43abe-...",
  "email": "user@example.com",
  "profileImage": "https://..."
}
```

---

### 1.5 `POST /api/latest/auth/loginWithProvider`

Authenticate or register via OAuth provider (Google, Azure AD).

**Request Body:**
```json
{
  "email": "user@example.com",
  "provider": "google",
  "sub": "google-oauth-sub-id",
  "profileImage": "https://..."  // optional
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "token": "eyJ...",
  "userId": "62b43abe-...",
  "email": "user@example.com"
}
```

---

### 1.6 `POST /api/latest/auth/onboarding`

Update onboarding details (requires auth).

**Auth:** Required (`verifyToken`)

**Request Body:**
```json
{
  "usage": "Business Intelligence",
  "fullName": "Jane Doe",
  "email": "jane@example.com",
  "role": "Data Analyst",
  "companyName": "Acme Corp",
  "industryType": "E-commerce",
  "companySize": "50-200",
  "country": "United States",
  "goals": "Build dashboards for sales tracking",
  "source": "Google Search"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "User onboarded successfully."
}
```

---

### 1.7 `GET /api/latest/auth/initiatePasswordReset?emailId={email}`

Send a password reset email.

**Query Params:** `emailId` (str)

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Password reset initiated successfully."
}
```

---

### 1.8 `PATCH /api/latest/auth/resetPassword`

Reset password with a reset token.

**Request Body:**
```json
{
  "email": "user@example.com",
  "newPassword": "newSecurePassword456"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Password updated successfully!"
}
```

---

### 1.9 `GET /api/latest/auth/logout`

End the current session.

**Auth:** Required (`verifyToken`)

**Response (200):**
```json
{
  "status": "SUCCESS"
}
```

---

## 2. Project Management

All routes under `/api/latest/projects/`. Auth via `verifyToken` unless otherwise noted.

### 2.1 `POST /api/latest/projects/createProject`

**Request Body:**
```json
{
  "projectName": "Q4 Sales Analysis",
  "projectDescription": "Tracking Q4 sales across regions",  // optional
  "workspaceId": "workspace-uuid",
  "domainExpert": "ecommerce"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "projectId": "effeaef7-50bf-412b-89c1-d523eba1028b",
  "message": "Project created successfully"
}
```

---

### 2.2 `POST /api/latest/projects/createWorkspace/{workspaceName}`

**Path Params:** `workspaceName` (str)

**Response (200):**
```json
{
  "status": "SUCCESS",
  "workspaceId": "workspace-uuid",
  "message": "Workspace created successfully"
}
```

---

### 2.3 `GET /api/latest/projects/listProjects/{workspaceId}`

**Response (200):**
```json
{
  "projects": [
    {
      "projectId": "effeaef7-...",
      "projectName": "Q4 Sales",
      "isBookmarked": 0,
      "isArchived": 0,
      "isTrash": 0,
      "domainExpert": "ecommerce",
      "modifiedAt": "2025-01-15T10:30:00Z"
    }
  ]
}
```

---

### 2.4 `GET /api/latest/projects/listWorkspaces`

**Response (200):**
```json
{
  "workspaces": [
    {
      "id": "workspace-uuid",
      "name": "Acme Corp",
      "current": true
    }
  ]
}
```

---

### 2.5 `PATCH /api/latest/projects/updateCurrentWorkspace/{updatedWorkspaceId}`

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Current workspace updated successfully"
}
```

---

### 2.6 `PATCH /api/latest/projects/updateWorkspaceName/{workspaceId}/{newWorkspaceName}`

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Workspace name updated successfully"
}
```

---

### 2.7 `DELETE /api/latest/projects/deleteWorkspace/{workspaceId}`

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Workspace and all associated projects deleted successfully"
}
```

---

### 2.8 `PATCH /api/latest/projects/updateBookmark`

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "action": "add"  // or "remove"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Project bookmark status updated successfully"
}
```

---

### 2.9 `PATCH /api/latest/projects/updateArchive`

**Request Body:** Same as `updateBookmark`

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Project archive status updated successfully"
}
```

---

### 2.10 `PATCH /api/latest/projects/updateTrash`

**Request Body:** Same as `updateBookmark`

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Project trash status updated successfully"
}
```

---

### 2.11 `DELETE /api/latest/projects/deleteProject?projectId={projectId}`

**Query Params:** `projectId` (str)

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Project deleted successfully"
}
```

---

### 2.12 `PATCH /api/latest/projects/renameProject`

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "newProjectName": "Q1 2026 Sales"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Project renamed successfully"
}
```

---

### 2.13 `GET /api/latest/projects/listTriggers/{projectId}`

**Response (200):**
```json
{
  "status": "SUCCESS",
  "triggers": [
    {
      "triggerId": "uuid",
      "triggerName": "Daily Report",
      "schedule": "0 9 * * *",
      "enabled": true
    }
  ]
}
```

---

### 2.14 `GET /api/latest/projects/listTriggersUnderUserId`

**Response (200):**
```json
{
  "status": "SUCCESS",
  "triggersAssignedToUser": [ ... ]
}
```

---

### 2.15 `GET /api/latest/projects/getUserProfile`

**Response (200):**
```json
{
  "status": "SUCCESS",
  "profile": {
    "userId": "62b43abe-...",
    "email": "user@example.com",
    "userName": "Jane Doe",
    "company": "Acme Corp",
    "position": "Data Analyst",
    "bio": "...",
    "profileImage": "https://...",
    "credits": {
      "monthlyCredits": 1000.0,
      "usedCredits": 240.0,
      "topupCredits": 50.0,
      "remainingCredits": 810.0,
      "usagePercentage": 24.0,
      "periodEnd": "2026-08-01T00:00:00+00:00",
      "initialized": true
    },
    "plan": {
      "planType": "monthly",
      "status": "ACTIVE",
      "planExpire": "2026-08-01T00:00:00+00:00",
      "nextBilling": "2026-08-01T00:00:00+00:00",
      "subscribedExperts": [],
      "domainCount": 0,
      "pendingRemovals": [],
      "subscriptionDaysLeft": 4,
      "billingMode": "recurring",
      "renewalDueAt": "2026-08-01T00:00:00+00:00"
    }
  }
}
```

> `credits` is a **trimmed, credits-only** view of the balance snapshot — just the
> fields the UI renders. Token counts, `planTier`, `periodStart`, and `lastResetAt`
> are omitted here (the full snapshot lives on `GET /credits/balance`). It is
> best-effort: on a credit-service error the field is `null` and the rest of the
> profile still returns.

---

### 2.16 `PUT /api/latest/projects/editUserProfile`

Multipart form upload.

**Form Fields:**
| Field | Type | Required |
|---|---|---|
| `userName` | str | No |
| `company` | str | No |
| `position` | str | No |
| `bio` | str | No |
| `profileImage` | file (or empty string) | No |

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Profile updated successfully",
  "profile": { ... }
}
```

---

## 3. Data Loading

All routes under `/api/latest/dataLoader/`. Auth via `verifyToken` unless noted.

### 3.1 `POST /api/latest/dataLoader/loadCsvData`

Multipart form upload.

**Form Fields:**
| Field | Type | Required |
|---|---|---|
| `projectId` | str | Yes |
| `files` | file[] (.csv) | Yes |

**Response (200):**
```json
{
  "status": "SUCCESS"
}
```

---

### 3.2 `POST /api/latest/dataLoader/loadExcelData`

**Form Fields:**
| Field | Type | Required |
|---|---|---|
| `projectId` | str | Yes |
| `files` | file[] (.xls, .xlsx) | Yes |

**Response (200):**
```json
{
  "status": "SUCCESS"
}
```

---

### 3.3 `POST /api/latest/dataLoader/loadPdfData`

**Auth:** Required + `requireCredits("pdf_extraction_per_page")`

**Form Fields:**
| Field | Type | Required |
|---|---|---|
| `projectId` | str | Yes |
| `files` | file[] (.pdf) | Yes |

**Response (200):**
```json
{
  "status": "SUCCESS"
}
```

---

### 3.4 `POST /api/latest/dataLoader/loadMySql`

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "user": "root",
  "password": "password",
  "host": "db.example.com",
  "port": 3306,
  "db": "sales_db",
  "table": "transactions"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS"
}
```

---

### 3.5 `POST /api/latest/dataLoader/loadPostgreSQL`

**Request Body:** Same shape as `loadMySql`

**Response (200):**
```json
{
  "status": "SUCCESS"
}
```

---

### 3.6 `POST /api/latest/dataLoader/loadMongoDB`

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "connectionString": "mongodb+srv://user:pass@cluster.mongodb.net",
  "db": "analytics",
  "collection": "events"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS"
}
```

---

### 3.7 `DELETE /api/latest/dataLoader/deleteTable`

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "tableName": "users"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Table deleted successfully"
}
```

---

## 4. Table Viewer

### 4.1 `GET /api/latest/projects/viewTable/{projectId}/{tableName}`

Read a parquet table and return paginated rows. Uses `pl.scan_parquet` for lazy row-range materialization — a 10M-row table paginates as fast as a 1k-row table.

**Auth:** Required (`verifyToken`)

**Path Params:**
| Param | Type | Required |
|---|---|---|
| `projectId` | str | Yes |
| `tableName` | str | Yes (without `.parquet` extension) |

**Query Params:**
| Param | Type | Default | Constraints |
|---|---|---|---|
| `page` | int | 1 | ≥ 1 |
| `pageSize` | int | 100 | 1–500 |

**Response (200):**
```json
{
  "status": "SUCCESS",
  "rows": [
    { "column1": "value1", "column2": 123 },
    { "column1": "value2", "column2": 456 }
  ],
  "pagination": {
    "page": 1,
    "pageSize": 100,
    "totalRows": 100000,
    "totalPages": 1000
  }
}
```

**Errors:** 401 (no auth), 403 (wrong owner), 500 (read failure)

---

## 5. Metadata & Insights

All routes under `/api/latest/projects/`.

### 5.1 `POST /api/latest/projects/generateMetadata/{projectId}`

Generate or refresh metadata for all tables in a project.

**Auth:** Required + `requireCredits("metadata_generation")`

**Response (200):**
```json
{
  "status": "SUCCESS",
  "metadata": {
    "users": {
      "description": "User demographics",
      "shape": [100000, 12],
      "columns": [...],
      "isActive": true
    },
    "orders": { "...isActive": true }
  }
}
```

---

### 5.2 `POST /api/latest/projects/generateKpis/{projectId}`

Generate insights/KPIs from project metadata.

**Auth:** Required + `requireCredits("insight_generation")`

**Query Params:** `preserveCharted` (bool, default `false`)

**Response (200):**
```json
{
  "status": "SUCCESS",
  "insights": [
    { "id": 1, "query": "Total revenue by month", "isCharted": false },
    { "id": 2, "query": "Top 10 customers by spend", "isCharted": false }
  ]
}
```

---

### 5.3 `GET /api/latest/projects/getInsights/{projectId}`

Retrieve saved insights for a project.

**Response (200):**
```json
{
  "insights": [
    { "id": 1, "query": "...", "isCharted": true }
  ]
}
```

---

### 5.4 `GET /api/latest/projects/getMetadata/{projectId}`

Retrieve all table metadata (active + inactive) for a project.

**Response (200):**
```json
{
  "tables": [
    {
      "tableName": "users",
      "tableDesc": "User demographics",
      "shape": [100000, 12],
      "columns": [...],
      "isActive": true
    }
  ]
}
```

---

### 5.5 `PUT /api/latest/projects/editMetadata`

Edit table or column description.

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "tableName": "users",
  "tableDescription": "Updated description",  // optional
  "columnName": "email",                       // optional
  "columnDescription": "User email address"    // optional
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "metadata": { ... }
}
```

---

### 5.6 `PATCH /api/latest/projects/toggleTableActive/{projectId}/{tableName}`

Toggle the `isActive` flag for a table. Inactive tables are hidden from all LLM-facing services.

**Auth:** Required (`verifyToken`)

**Response (200):**
```json
{
  "status": "SUCCESS",
  "tableName": "old_data",
  "isActive": false
}
```

**Errors:**
- `404` — Table not found in metadata

---

### 5.7 `POST /api/latest/projects/generateReport/{projectId}`

Generate a profiling report (ydata-profiling) for all tables in a project.

**Response (200):**
```json
{
  "status": "SUCCESS",
  "reportHtmlContent": "<html>...</html>"
}
```

---

### 5.8 `GET /api/latest/projects/getReport/{projectId}/{tableName}`

Get the HTML profiling report for a specific table.

**Response (200):** `text/html` — full HTML report.

---

## 6. Reporting & Charts

All routes under `/api/latest/reportingTool/`.

### 6.1 `POST /api/latest/reportingTool/generateChart`

Generate a single chart via the LLM workflow.

**Auth:** Required + `requireCredits("reporting_query")`

**Request Body:**
```json
{
  "inputQuery": "Show total revenue by month for 2025",
  "projectId": "effeaef7-..."
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "chartData": {
    "labels": ["Jan", "Feb", "Mar"],
    "datasets": [{ "label": "Revenue", "data": [10000, 12000, 15000] }]
  },
  "generatedCode": "import polars as pl..."
}
```

---

### 6.2 `POST /api/latest/reportingTool/generatePanelChart`

Generate a panel chart (multiple charts in one view) without LLM.

**Auth:** Required (`verifyToken`)

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "chartType": "bar",
  "xAxis": "month",
  "yAxis": "revenue",
  "dataSource": "users",
  "aggregationMetric": "sum",
  "index": ["month"],
  "columns": ["revenue"],
  "values": ["revenue"],
  "isFilterApplied": false,
  "filters": [],
  "mapType": null,
  "selectedColumns": null,
  "zipCodeColumn": null
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "chartData": { ... }
}
```

---

### 6.3 `POST /api/latest/reportingTool/generateAndExportChartsInParallel`

Generate and export multiple charts in parallel.

**Auth:** Required + `requireCredits("reporting_query")`

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "inputQueries": [
    "Show revenue by month",
    "Show top customers by spend"
  ]
}
```

**Response (200):**
```json
{
  "message": "Charts generated successfully",
  "pageData": [ ... ]
}
```

---

### 6.4 `POST /api/latest/reportingTool/saveQuery`

Save a favourite query.

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "query": "SELECT * FROM users WHERE age > 30"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Query saved successfully.",
  "queryId": "query-uuid"
}
```

---

### 6.5 `GET /api/latest/reportingTool/getQueries/{projectId}`

**Response (200):**
```json
{
  "status": "SUCCESS",
  "queries": {
    "query-uuid": "SELECT * FROM users..."
  }
}
```

---

### 6.6 `DELETE /api/latest/reportingTool/deleteQuery`

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "queryId": "query-uuid"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Query deleted successfully."
}
```

---

## 7. Data Blends

All routes under `/api/latest/blends/`.

### 7.1 `POST /api/latest/blends/createDataBlend`

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "blendOn": ["user_id"],
  "blendName": "users_orders",
  "tables": ["users", "orders"],
  "joinTypes": ["inner"]
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Blend created successfully."
}
```

---

### 7.2 `GET /api/latest/blends/getDataSources?projectId={projectId}`

**Response (200):**
```json
{
  "blends": [
    { "blendName": "users_orders", "tables": ["users", "orders"], "joinTypes": ["inner"] }
  ],
  "rawTables": ["users", "orders", "products"],
  "blendedTables": ["users_orders"]
}
```

> Only **active** tables are included in `rawTables`.

---

### 7.3 `POST /api/latest/blends/getFieldsFromSources`

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "tableName": "users"
}
```

**Response (200):**
```json
{
  "numerical": ["age", "family_size"],
  "categorical": ["gender", "occupation"],
  "datetime": ["created_at"]
}
```

> Returns `403` if the table is inactive.

---

## 8. Dashboard

All routes under `/api/latest/dashboard/`.

### 8.1 `POST /api/latest/dashboard/createPage`

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "pageName": "Sales Overview"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "pageId": "page-uuid"
}
```

---

### 8.2 `GET /api/latest/dashboard/getAllPages?projectId={projectId}`

**Response (200):**
```json
{
  "status": "SUCCESS",
  "pages": [
    { "pageId": "uuid", "pageName": "Sales Overview", "widgets": [...] }
  ]
}
```

---

### 8.3 `POST /api/latest/dashboard/exportToDashboard`

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "page": "page-uuid",
  "chartType": "bar",
  "title": "Revenue 2025",
  "label": "Revenue",
  "xLabels": ["Q1", "Q2"],
  "yLabels": ["10000", "12000"],
  "data": { "Revenue": [10000, 12000] },
  "map": null,
  "layout": { "x": 0, "y": 0, "w": 6, "h": 4 },
  "generatedCode": "import polars as pl..."
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "widgetId": "widget-uuid"
}
```

---

### 8.4 `POST /api/latest/dashboard/getData`

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "page": "page-uuid",
  "filters": [],
  "refresh": false
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "pageData": {
    "pageId": "uuid",
    "pageName": "Sales Overview",
    "widgets": [...]
  }
}
```

---

### 8.5 `PUT /api/latest/dashboard/editWidgetPosition`

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "pageId": "page-uuid",
  "pageName": "Sales Overview",
  "widgets": [
    { "widgetId": "uuid", "x": 0, "y": 0, "w": 6, "h": 4 }
  ]
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "pageData": { ... }
}
```

---

### 8.6 `DELETE /api/latest/dashboard/deleteDashboardElement`

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "deletionObject": "widget",
  "id": "widget-uuid"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "element deleted successfully."
}
```

---

### 8.7 `GET /api/latest/dashboard/getAllColumns/{projectId}`

**Response (200):**
```json
{
  "status": "SUCCESS",
  "details": [
    { "tableName": "users", "columns": [...] }
  ]
}
```

---

### 8.8 `GET /api/latest/dashboard/dashboardRefresh/{projectId}`

Refresh all widget data in parallel.

**Response (200):**
```json
{
  "message": "Data refreshed successfully.",
  "pageData": [ ... ]
}
```

---

## 9. Transformations

All routes under `/api/latest/transformations/`.

### 9.1 `POST /api/latest/transformations?projectId={projectId}`

Create a new transformation workspace.

**Auth:** Required + `requireActiveSubscription`

**Request Body:**
```json
{
  "transformation_name": "Cleaned Users",
  "description": "Remove nulls and standardize names"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "transformationId": "trans-uuid"
}
```

---

### 9.2 `GET /api/latest/transformations?projectId={projectId}`

List all transformations for a project.

**Response (200):**
```json
{
  "data": [
    {
      "transformationId": "uuid",
      "transformationName": "Cleaned Users",
      "description": "...",
      "latestApprovedArtifact": { "mermaid_code": "...", "message_id": "...", "table_name": "cleaned_users" },
      "createdAt": "2025-01-15T...",
      "updatedAt": "2025-01-15T..."
    }
  ]
}
```

---

### 9.3 `GET /api/latest/transformations/{transformationId}/messages?projectId={projectId}`

Get chat history for a transformation.

**Response (200):**
```json
{
  "messages": [
    {
      "messageId": "msg-uuid",
      "transformationId": "trans-uuid",
      "role": "user",
      "content": "Clean the users table",
      "artifact": null,
      "createdAt": "2025-01-15T..."
    },
    {
      "messageId": "msg-uuid-2",
      "transformationId": "trans-uuid",
      "role": "assistant",
      "content": "Here's a transformation...",
      "artifact": {
        "type": "mermaid",
        "code": "graph LR\n  A[users] --> B[cleaned_users]",
        "isApproved": false
      },
      "createdAt": "2025-01-15T..."
    }
  ]
}
```

---

### 9.4 `POST /api/latest/transformations/{transformationId}/messages?projectId={projectId}`

Send a user message and stream the assistant response via SSE.

**Auth:** Required + `requireCredits("transformation_message")`

**Request Body:**
```json
{
  "content": "Also remove duplicate emails"
}
```

**Response:** `text/event-stream` — SSE events with assistant response chunks.

---

### 9.5 `POST /api/latest/transformations/{transformationId}/messages/{messageId}?projectId={projectId}`

Approve a Mermaid artifact and return a 100-row preview of the transformed table. Validates the table name doesn't already exist in metadata.

**Auth:** Required + `requireActiveSubscription`

**Response (200):**
```json
{
  "data": [
    { "user_id": 1, "name": "Alice", "email": "alice@example.com" }
  ]
}
```

---

### 9.6 `POST /api/latest/transformations/{transformationId}/messages/{messageId}/apply?projectId={projectId}`

Persist the approved transformation as a project table. Uploads parquet to Supabase storage and updates metadata.

**Auth:** Required + `requireActiveSubscription`

**Response (200):**
```json
{
  "status": "200",
  "message": "Transformation applied successfully. Table 'cleaned_users' is now available."
}
```

---

### 9.7 `POST /api/latest/transformations/{transformationId}/rollback?projectId={projectId}`

Rollback workspace state and messages to a specific message ID.

**Auth:** Required + `requireActiveSubscription`

**Request Body:**
```json
{
  "messageId": "msg-uuid"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Rolled back successfully."
}
```

---

### 9.8 `PATCH /api/latest/transformations/{transformationId}/rename?projectId={projectId}`

**Request Body:**
```json
{
  "newTransformationName": "Cleaned Users v2"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Transformation renamed successfully."
}
```

---

### 9.9 `DELETE /api/latest/transformations/{transformationId}?projectId={projectId}`

Delete a transformation workspace and its associated parquet file.

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Transformation deleted successfully."
}
```

---

## 10. Utility Endpoints

All routes under `/api/latest/utils/`.

### 10.1 `POST /api/latest/utils/getSpeechTranscript`

Transcribe a base64-encoded audio file.

**Auth:** Required + `requireCredits("speech_to_text")`

**Request Body:**
```json
{
  "b64String": "data:audio/wav;base64,UklGRi..."
}
```

**Response (200):**
```json
{
  "transcriptionText": "Hello, this is a test recording."
}
```

---

### 10.2 `POST /api/latest/utils/getInsightsFromImage`

Extract structured insights from a dashboard page's **data** — widget data,
provenance, per-widget statistical signals, and schema — not a screenshot. The
endpoint keeps its historical name for backward compatibility.

**Auth:** Required + `requireCredits("image_to_insights")`

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "pageId": "page-uuid",  // optional: dashboard page to analyse
  "refresh": false,       // optional: false uses cache, true regenerates
  "mode": "data",         // optional: "data" (default) or "hybrid"
  "b64String": null       // optional: only used when mode is "hybrid"
}
```

`mode: "data"` (default, recommended) sends only the structured dashboard data.
`mode: "hybrid"` additionally attaches a base64 screenshot in `b64String` as
supplementary layout context; the data payload stays authoritative. Send
`refresh: false` on a normal dashboard open (serves the cached insight when the
page's data is unchanged) and `refresh: true` to force regeneration.

**Response (200):**
```json
{
  "insights": {
    "diagnostic_insights": [
      {
        "finding": "Conversion rate dropped by 12% in the last 7 days.",
        "evidence": "Conversion rate: 3.2% -> 2.8% — period change -12.4% (W2 signals).",
        "widget_ref": "W2",
        "business_impact": "Direct revenue loss of approximately $5,000 per day.",
        "confidence": 0.95
      }
    ],
    "prescriptive_actions": [
      {
        "recommended_action": "Audit the checkout page for layout changes or errors.",
        "expected_impact": "Stabilization of conversion rates to baseline (3.2%).",
        "owner": "UX Design Team",
        "priority": "high"
      }
    ],
    "missing_data": ["Attribution data for paid traffic is missing."]
  },
  "source": "cache",
  "cacheHit": true,
  "insightId": "7eb09ae7-a801-40f0-b9f9-fbd97c52dc16",
  "generatedAt": "2026-06-09T00:00:00+00:00"
}
```

---

### 10.3 `GET /api/latest/utils/getDashboardInsights/{projectId}`

**Response (200):**
```json
{
  "insights": [
    {
      "insightId": "insight-uuid",
      "projectId": "effeaef7-...",
      "status": "new",
      "content": { "diagnostic_insights": [...] },
      "createdAt": "2025-01-15T..."
    }
  ]
}
```

---

### 10.4 `PATCH /api/latest/utils/updateDashboardInsightStatus`

**Request Body:**
```json
{
  "projectId": "effeaef7-...",
  "insightId": "insight-uuid",
  "status": "accepted"  // or "rejected", "implemented"
}
```

**Response (200):**
```json
{
  "insight": { ... }
}
```

---

### 10.5 `GET /api/latest/utils/sendForecasts`

Trigger a forecasts Celery task.

**Response (200):**
```json
{
  "taskId": "celery-task-uuid",
  "triggerName": "forecast",
  "taskStatus": "PENDING"
}
```

---

### 10.6 `WS /api/latest/utils/ws/getTaskStatus?token={jwt}&taskId={taskId}`

WebSocket for polling Celery task status. Emits JSON status updates every 5 seconds until the task is ready.

**Connection:**
```
ws://api.example.com/api/latest/utils/ws/getTaskStatus?token=eyJ...&taskId=celery-uuid
```

**Messages received:**
```json
{ "status": "RUNNING" }
{ "taskId": "uuid", "status": "SUCCESS", "taskResponseCode": { ... } }
```

---

## 11. Subscriptions

All routes under `/api/latest/subscriptions/`. Auth via `verifyToken`.

### 11.1 `GET /api/latest/subscriptions/activateFreeTrial`

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Free trial activated successfully.",
  "data": { "trialEndsAt": "2025-01-30T..." }
}
```

---

### 11.2 `POST /api/latest/subscriptions/createSubscription`

**Request Body:**
```json
{
  "domains": ["example.com", "example.org"],
  "contact": "user@example.com",
  "billingMode": "monthly_recurring"  // or "annual"
}
```

**Response (200):**
```json
{
  "orderId": "razorpay-order-id",
  "amount": 5000,
  "currency": "INR",
  "key": "razorpay_key_id"
}
```

---

### 11.3 `POST /api/latest/subscriptions/verifySubscription`

**Request Body:**
```json
{
  "razorpayOrderId": "order-id",
  "razorpayPaymentId": "payment-id",
  "razorpaySignature": "signature-hash"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Subscription verified successfully."
}
```

---

### 11.4 `POST /api/latest/subscriptions/addDomains`

**Request Body:**
```json
{
  "domains": ["newdomain.com"]
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Domain upgrade order created.",
  "data": { "orderId": "...", "amount": 1000 }
}
```

---

### 11.5 `POST /api/latest/subscriptions/verifyDomainUpgrade`

**Request Body:**
```json
{
  "domains": ["newdomain.com"],
  "razorpayOrderId": "...",
  "razorpayPaymentId": "...",
  "razorpaySignature": "..."
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Domain upgrade verified and activated."
}
```

---

### 11.6 `POST /api/latest/subscriptions/removeDomain`

**Request Body:**
```json
{
  "domains": ["example.org"]
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "1 domain(s) scheduled for removal at cycle end.",
  "data": { "currentDomains": [...], "pendingRemovals": [...] }
}
```

---

### 11.7 `POST /api/latest/subscriptions/cancelPendingAddition`

**Request Body:**
```json
{
  "domain": "newdomain.com"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Pending domain addition cancelled."
}
```

---

### 11.8 `POST /api/latest/subscriptions/cancelSubscription`

**Request Body:**
```json
{
  "reason": "Too expensive"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Subscription will be cancelled at the end of the current billing cycle."
}
```

---

### 11.9 `POST /api/latest/subscriptions/refund`

**Request Body:**
```json
{
  "paymentId": "razorpay-payment-id",
  "amount": 5000  // optional, in paise. Omit for full refund.
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Refund initiated successfully.",
  "data": { "refundId": "..." }
}
```

---

### 11.10 `GET /api/latest/subscriptions/invoices`

**Response (200):**
```json
{
  "status": "SUCCESS",
  "invoices": [
    {
      "invoiceId": "inv-uuid",
      "amount": 5000,
      "currency": "INR",
      "status": "paid",
      "createdAt": "2025-01-01T..."
    }
  ]
}
```

---

### 11.11 `POST /api/latest/subscriptions/createAnnualRenewalPaymentSession`

**Request Body:**
```json
{
  "invoiceId": "inv-uuid"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Annual renewal payment session created.",
  "data": { "orderId": "...", "amount": 60000 }
}
```

---

### 11.12 `POST /api/latest/subscriptions/verifyAnnualRenewalPayment`

**Request Body:**
```json
{
  "invoiceId": "inv-uuid",
  "razorpayOrderId": "...",
  "razorpayPaymentId": "...",
  "razorpaySignature": "..."
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "message": "Annual renewal payment verified and finalized.",
  "data": { "finalized": true }
}
```

---

## 12. Billing Admin

All routes under `/api/latest/billingAdmin/`. Auth via `verifyBillingAdmin` (requires `BILLING_ADMIN_USER_IDS` env var).

### 12.1 `GET /api/latest/billingAdmin/reconciliation/report`

**Response (200):**
```json
{
  "status": "SUCCESS",
  "data": {
    "staleAttempts": [...],
    "mismatches": [...],
    "webhookAnomalies": [...]
  }
}
```

---

### 12.2 `GET /api/latest/billingAdmin/metrics`

**Response (200):**
```json
{
  "status": "SUCCESS",
  "data": {
    "metrics": { "mrr": 50000, "activeSubscriptions": 120, ... },
    "alerts": [{ "level": "warning", "message": "..." }]
  }
}
```

---

### 12.3 `POST /api/latest/billingAdmin/reconciliation/webhooks/replay`

**Request Body:**
```json
{
  "eventId": "webhook-event-id"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "data": { "replayed": true, "result": "..." }
}
```

---

### 12.4 `POST /api/latest/billingAdmin/reconciliation/mark-investigated`

**Request Body:**
```json
{
  "entityType": "subscription",
  "entityId": "sub-uuid",
  "note": "Customer confirmed payment via email"
}
```

**Response (200):**
```json
{
  "status": "SUCCESS",
  "data": { "entityId": "sub-uuid", "investigatedAt": "2025-01-15T..." }
}
```

---

### 12.5 `POST /api/latest/billingAdmin/credits/force-reset?resetUsage={true|false}`

Recompute `monthly_token_quota` for all users from `credits.json` and flush Redis credit hashes.

**Query Params:** `resetUsage` (bool, default `false`) — when `true`, also zero `used_tokens` and restore `remaining_tokens` to the full quota for every user, giving everyone a fresh monthly bucket immediately. The billing period is left untouched.

**Response (200):**
```json
{
  "status": "SUCCESS",
  "data": {
    "updatedCount": 150,
    "redisDeletedCount": 148,
    "mode": "quotaSync"
  }
}
```

---

## 13. Webhooks

### 13.1 `POST /api/latest/webhooks/razorpay`

Razorpay webhook receiver. Verifies `X-Razorpay-Signature` HMAC SHA-256, then dispatches the event for processing.

**Auth:** None (signature-verified)

**Headers:**
| Header | Required | Description |
|---|---|---|
| `X-Razorpay-Signature` | Yes | HMAC SHA-256 of the body using webhook secret |
| `X-Razorpay-Event-Id` | No | Event ID for idempotency |

**Request Body:** Raw Razorpay event JSON

**Response (200):**
```json
{
  "status": "ok"
}
```

**Errors:**
- `400` — Invalid signature or malformed JSON
- `503` — Retryable error (Razorpay will retry)

---

## 14. Credits

All routes under `/api/latest/credits/`. Auth via `verifyUser`.

### 14.1 `GET /api/latest/credits/balance`

Get the current balance snapshot. Balances are tracked in raw LLM tokens; credit
fields are derived at 10,000 tokens per credit and returned as floats (2 dp).

**Response (200):**
```json
{
  "status": "SUCCESS",
  "data": {
    "planTier": "pro",
    "monthlyTokenQuota": 10000000,
    "usedTokens": 480000,
    "remainingTokens": 9520000,
    "monthlyCredits": 1000.0,
    "usedCredits": 48.0,
    "remainingCredits": 952.0,
    "usagePercentage": 4.8,
    "periodStart": "2026-07-01T00:00:00Z",
    "periodEnd": "2026-08-01T00:00:00Z",
    "lastResetAt": "2026-07-01T00:00:00Z",
    "initialized": true
  }
}
```

`initialized` is `false` with all counters at zero when the user has no
`credit_balances` row yet.

---

### 14.2 `GET /api/latest/credits/usage`

Get token usage summary with optional Langfuse breakdown by operation and model.

**Response (200):**
```json
{
  "status": "SUCCESS",
  "data": {
    "totalUsedTokens": 480000,
    "monthlyTokenQuota": 10000000,
    "remainingTokens": 9520000,
    "totalUsedCredits": 48.0,
    "monthlyCredits": 1000.0,
    "remainingCredits": 952.0,
    "usagePercentage": 4.8,
    "periodStart": "2026-07-01T00:00:00Z",
    "periodEnd": "2026-08-01T00:00:00Z",
    "planTier": "pro",
    "langfuseAvailable": true,
    "breakdown": {
      "byOperation": [
        { "tag": "reporting_query", "totalTokens": 260000, "callCount": 12 }
      ],
      "byModel": [
        { "model": "gemini-2.0-flash", "totalTokens": 180000, "callCount": 20 }
      ]
    }
  }
}
```

`breakdown` arrays are empty and `langfuseAvailable` is `false` when Langfuse is
unconfigured or its metrics endpoint returns an unexpected shape.

---

## 15. Admin Panel

All routes under `/api/latest/admin/` use the isolated administrator bearer
token issued by `POST /api/latest/admin/auth/login`.

### 15.1 `PATCH /api/latest/admin/users/{userId}/access`

Ban a product user or restore their access. Banning is effective immediately:
the authoritative flag is written to `Users`, all Nubrix product sessions are
revoked, and Supabase Auth is synchronized as a defense-in-depth control.

**Request:**
```json
{
  "banned": true,
  "reason": "Optional internal reason"
}
```

`reason` is optional, accepts `null` or an empty string, and is limited to
1,000 characters. It is retained for administrators and audit history but is
never returned to the banned user.

To restore access:
```json
{
  "banned": false
}
```

Restoring access does not restore any prior session. Residual sessions are
revoked before the ban flag is cleared, and the user must log in again to
receive a fresh token. If residual session cleanup fails, the user remains
banned and the endpoint returns `500`.

**Response (200):**
```json
{
  "userId": "2f5a3428-82a5-41d9-ae80-5388842953bc",
  "isBanned": true,
  "bannedAt": "2026-08-23T15:39:03+00:00",
  "bannedBy": "f1998e72-e414-4694-8873-211b2136f25f",
  "banReason": null,
  "sessionsRevoked": 2,
  "supabaseAuthSynced": true,
  "warnings": []
}
```

If Nubrix session cleanup or Supabase Auth synchronization fails, the database
ban remains authoritative. The response contains a safe warning and the audit
outcome is recorded as `side_effect_failed`.

**Errors:**
- `401` — Missing, invalid, expired, or revoked administrator session
- `404` — Product user not found
- `422` — Invalid payload or reason longer than 1,000 characters
- `500` — The authoritative state could not be written, or sessions could not
  be safely revoked before restoring access

When a banned user attempts login or uses a previously issued token, the
product API returns `403` with `errorCode: "ACCOUNT_ACCESS_REVOKED"` and tells
the user to contact NubrixAI Support.

Access changes target exactly one user per request. There is no batch
ban/restore endpoint, and clients must not emulate one with automatic request
loops.

### 15.2 `POST /api/latest/admin/users/{userId}/erasure`

Start the automatic, durable user-erasure workflow. The endpoint first applies
the authoritative access ban, freezes billing, persists all workflow steps,
and queues Celery.

**Headers:**

- `Authorization: Bearer <admin-token>`
- `Idempotency-Key: <UUID>`

**Request:**

```json
{
  "confirmation": "ERASE",
  "reason": "Optional internal reason"
}
```

`confirmation` must be the literal `ERASE`. `reason` is optional, blank values
normalize to `null`, and the maximum length is 1,000 characters.

**Response (202):**

```json
{
  "requestId": "8cfdb150-417d-47ab-acd1-fef39d2bc14e",
  "status": "PENDING",
  "userId": "2f5a3428-82a5-41d9-ae80-5388842953bc",
  "createdAt": "2026-08-24T10:00:00+00:00"
}
```

Reusing the same idempotency key for the same user returns the same request.
Reusing it for another user returns `409`. A user may have only one active
erasure request. The workflow automatically cleans owned database data,
Supabase Storage and Auth, Redis/cache state, and local billing credentials;
retained billing/audit records are anonymized.

The returned `requestId` is for audit and log correlation. There is no erasure
status, batch-erasure, or manual-retry API. Celery and the recovery sweep retry
the durable workflow automatically. Sanitized `user.erasure.complete` and
`user.erasure.failed` outcomes are available through the existing
`GET /api/latest/admin/audit?targetType=user_erasure_request` endpoint.

**Errors:** `401`, `404`, `409`, `422`, or `500`.

### 15.3 `POST /api/latest/admin/free-trial/extensions`

Add 1–30 days to one free-trial subscription and refresh that user's included
free credits in the same operation.

**Headers:**

- `Authorization: Bearer <admin-token>`
- `Idempotency-Key: <UUID>`

**Request:**

```json
{
  "userId": "free-user",
  "days": 5,
  "reason": "Optional internal reason"
}
```

`userId` is required, trimmed, must not be blank, and is limited to 128
characters. `days` must be a JSON integer from 1 through 30. `reason` is
optional, blank normalizes to `null`, and the maximum length is 1,000
characters.

Only subscriptions with `billing_mode=none`, `plan_type=free`, and status
`trial` or `expired` are eligible. Active trials stack from their existing
expiry; expired and stale trials restart from the current UTC time. An
ineligible request returns a single `FAILED` result without modifying the
subscription.

**Response (200):**

```json
{
  "extensionId": "f53b33cd-219e-4c70-b5c2-43d956591fa5",
  "userId": "free-user",
  "outcome": "EXTENDED",
  "daysAdded": 5,
  "previousExpiry": "2026-08-25T10:00:00+00:00",
  "newExpiry": "2026-08-30T10:00:00+00:00",
  "creditsRefreshed": true,
  "creditSyncStatus": "SYNCED",
  "accessStillBanned": false,
  "errorCode": null
}
```

A successful extension fully refreshes the user's free allowance, resets used
monthly tokens to zero, and restarts the credit period while preserving purchased
top-up tokens. A temporary Redis failure leaves the durable extension successful
with `creditSyncStatus: PENDING`; Celery retries automatically. A banned user
stays banned. Generation fencing marks an older queued cache write `SUPERSEDED`,
and erasure can mark an unsafe pending write `CANCELLED`.

`outcome` is `EXTENDED` or `FAILED`. Failure codes are `USER_NOT_FOUND`,
`SUBSCRIPTION_NOT_FOUND`, `USER_ERASURE_PENDING`,
`PAID_SUBSCRIPTION_NOT_ELIGIBLE`, `FREE_TRIAL_NOT_ELIGIBLE`, and
`EXTENSION_FAILED`. The same idempotency key and canonical payload replay safely;
a different payload with the same key returns `409`.

**Errors:** `401`, `409`, `422`, or `500` when the extension operation cannot be
persisted.

### 15.4 `POST /api/latest/admin/free-trial/reductions`

Remove 1–30 days from one active free trial without changing its plan, status,
start date, entitlements, or credits. This endpoint cannot end a trial
immediately.

**Headers:**

- `Authorization: Bearer <admin-token>`
- `Idempotency-Key: <UUID>`

**Request:**

```json
{
  "userId": "free-user",
  "days": 3,
  "reason": "Abuse remediation",
  "confirmation": "REDUCE"
}
```

`userId` is required, trimmed, must not be blank, and is limited to 128
characters. `days` must be a strict JSON integer from 1 through 30. `reason`
is required, trimmed, must not be blank, and is limited to 1,000 characters.
`confirmation` must be the exact, case-sensitive string `REDUCE`.

Only subscriptions with `billing_mode=none`, `plan_type=free`, status `trial`,
and a future `current_period_end` qualify. The service subtracts exact 24-hour
periods from the existing expiry. If the resulting expiry would be at or before
the current UTC time, it returns a durable `FAILED` result and leaves the
subscription unchanged. Expired trials cannot be reduced or reactivated.

**Response (200):**

```json
{
  "reductionId": "9e3d768e-9f92-45f4-b816-2a9937ec97f8",
  "userId": "free-user",
  "outcome": "REDUCED",
  "daysRemoved": 3,
  "previousExpiry": "2026-09-20T10:00:00+00:00",
  "newExpiry": "2026-09-17T10:00:00+00:00",
  "accessStillBanned": false,
  "errorCode": null
}
```

A successful operation updates `current_period_end`, `renewal_due_at`, the
lifecycle snapshot, and the subscription version in one transaction. It does
not refresh or reduce credits, alter the trial start, or remove an access ban.
The per-user advisory lock is shared with trial extensions, so concurrent
extension, reduction, and erasure operations serialize safely.

`outcome` is `REDUCED` or `FAILED`. Failure codes are `USER_NOT_FOUND`,
`SUBSCRIPTION_NOT_FOUND`, `USER_ERASURE_PENDING`,
`PAID_SUBSCRIPTION_NOT_ELIGIBLE`, `FREE_TRIAL_NOT_ACTIVE`,
`INVALID_TRIAL_EXPIRY`, `REDUCTION_WOULD_EXPIRE_TRIAL`, and
`REDUCTION_FAILED`. The same idempotency key and canonical payload replay
safely; a different payload with the same key returns `409`.

Before a ledger row is admitted, a missing user returns `404 User not found`
and an erasure-fenced user returns `409 User erasure is in progress`. These
pre-admission rejections deliberately create neither a reduction ledger nor a
targeted reduction audit event, preventing a request from recreating an
identity-bearing record after erasure. The same conditions discovered after
admission remain durable `FAILED` outcomes.

**Errors:** `401`; `404 User not found`; `409 Idempotency key is already in
use`; `409 User erasure is in progress`; `422`; or `500` when the reduction
operation cannot be persisted.

### 15.5 `GET /api/latest/admin/overview/user-signups`

Return new-user signup counts bucketed over a trailing time period, shaped for a
line chart. Values are computed fresh on every request, so the frontend can poll
it (React Query `refetchInterval`) for a live view.

**Headers:**

- `Authorization: Bearer <admin-token>`

**Query parameters:**

- `period` — one of `7d`, `14d`, `30d`, `90d`, `6m`, `1y`. Defaults to `30d`.

Bucket granularity is derived from the period, so the client only sends a
period:

| `period` | `granularity` | Buckets | Label format |
| --- | --- | --- | --- |
| `7d`, `14d`, `30d` | `day` | 7, 14, 30 | `YYYY-MM-DD` |
| `90d` | `week` | 13 | `YYYY-MM-DD` (week start) |
| `6m` | `week` | 26 | `YYYY-MM-DD` (week start) |
| `1y` | `month` | 12 | `YYYY-MM` |

Weekly periods are rounded to whole weeks (13 and 26 seven-day buckets) so every
bucket spans the same number of days. Daily buckets are UTC calendar days ending
with the current day; monthly buckets are UTC calendar months ending with the
current month.

**Response:**

```json
{
  "period": "30d",
  "granularity": "day",
  "timezone": "UTC",
  "rangeStart": "2026-07-28T00:00:00+00:00",
  "rangeEnd": "2026-08-27T00:00:00+00:00",
  "lastUpdatedAt": "2026-08-26T14:30:00+00:00",
  "totalSignups": 128,
  "chart": {
    "labels": ["2026-07-28", "2026-07-29"],
    "datasets": [{ "label": "New users", "data": [1, 3] }]
  }
}
```

`chart` matches the Chart.js-shaped wire contract the frontend already consumes
(`ChartData` in `src/types/chartTypes.ts`), so it can be passed straight to the
line chart component with no reshaping. Buckets with no signups are zero-filled
so the line has no gaps. `rangeEnd` is exclusive. `lastUpdatedAt` is the instant
the response was computed, for a freshness indicator between polls.

**Errors:** `401`, `422` for an unsupported `period`, or a safe `500` read
failure.

### 15.6 `GET /api/latest/admin/overview/token-usage`

Return provider-reported LLM token usage from Langfuse, bucketed for the same
line-chart filters as the signup overview. Values are queried fresh on every
request so the frontend can use the same polling interval as the signup chart.
This metric does not represent non-LLM estimated charges.

**Headers:**

- `Authorization: Bearer <admin-token>`

**Query parameters:**

- `period` — one of `7d`, `14d`, `30d`, `90d`, `6m`, `1y`. Defaults to `30d`.

Period granularity, bucket counts, UTC boundaries, and label formats match
`GET /api/latest/admin/overview/user-signups`.

**Response:**

```json
{
  "period": "30d",
  "granularity": "day",
  "timezone": "UTC",
  "rangeStart": "2026-07-28T00:00:00+00:00",
  "rangeEnd": "2026-08-27T00:00:00+00:00",
  "lastUpdatedAt": "2026-08-26T14:30:00+00:00",
  "totalTokens": 750,
  "chart": {
    "labels": ["2026-07-28", "2026-07-29"],
    "datasets": [{ "label": "LLM tokens", "data": [250, 500] }]
  }
}
```

Days without usage are zero-filled. Langfuse is queried at daily granularity
and the API rolls those values into the signup chart's weekly or monthly
buckets when required.

**Errors:** `401`, `422` for an unsupported `period`, `503` when Langfuse is not
configured, or a safe `500` when the Langfuse Metrics API request fails.

---

## Common Error Responses

All endpoints (except webhook) return errors in a flat shape:
```json
{
  "status": "FAILURE",
  "message": "Human-readable error message"
}
```

| HTTP Code | Meaning |
|---|---|
| `400` | Bad request / validation error |
| `401` | Missing or invalid token |
| `403` | Authenticated but not authorised (e.g., not project owner) |
| `404` | Resource not found |
| `409` | Conflict (e.g., duplicate table name) |
| `422` | Unprocessable entity (semantic validation) |
| `500` | Internal server error |
| `503` | Service unavailable (retry later) |

---

## Authentication

All endpoints (except `/auth/signUp`, `/auth/login`, `/auth/loginWithProvider`, `/auth/confirmMail/{userId}`, `/auth/initiatePasswordReset`, `/webhooks/razorpay`) require a valid JWT in the `Authorization: Bearer <token>` header.

JWT requirements:
- Algorithm: HS256
- Secret: `SECRET_KEY` env var
- Required claims: `email`, `userId`
- Optional claims: `exp`, `sub_status`, `plan_type`

---

## Credit-Gated Endpoints

The following endpoints consume credits and return `402 Payment Required` when the user has insufficient balance:

| Endpoint | Operation Key |
|---|---|
| `POST /projects/generateMetadata/{projectId}` | `metadata_generation` |
| `POST /projects/generateKpis/{projectId}` | `insight_generation` |
| `POST /reportingTool/generateChart` | `reporting_query` |
| `POST /reportingTool/generateAndExportChartsInParallel` | `reporting_query` |
| `POST /transformations/.../messages` | `transformation_message` |
| `POST /utils/getSpeechTranscript` | `speech_to_text` |
| `POST /utils/getInsightsFromImage` | `image_to_insights` |
| `POST /dataLoader/loadPdfData` | `pdf_extraction_per_page` |

---

## Rate Limiting & Concurrency

- Per-tenant API concurrency cap: 50 concurrent heavy requests per project (soft limit, Redis ZSET semaphore).
- Sandbox code execution: subprocess isolation with `close_fds=True` (no fork, no inherited sockets).
- Transformation execution: subprocess + temp-file IPC, Polars head-only preview.
