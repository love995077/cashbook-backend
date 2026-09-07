We are ready to implement the next major phase of the Cashbook application, focusing on Worker Self-Service, advanced UI features, and Theme Management. Please update the FastAPI backend and React Native frontend with the following specifications.

Phase 6: Worker Self-Service & UI Utilities
Part 1: Backend Schema & APIs

Advance Requests: Create a new advance_requests table (id, worker_id, amount, reason, status [pending/approved/rejected], created_at). Create POST (for workers to request) and PATCH (for contractors to approve/deny) endpoints.

Disputes: Add an is_disputed boolean column to the existing attendance table. Add a PATCH /attendance/{id}/dispute endpoint for workers to flag an entry.

Provide the Alembic migration commands to apply these schema updates.

Part 2: Worker Frontend (React Native)

Digital QR Identity: Display a large QR code on the Worker Dashboard encoding their worker_id.

Request Advance UI: Add a "Request Advance" button opening a modal to input an amount and reason.

Site & Maps: Display their currently assigned site name on the dashboard with a button that uses Linking to open Google Maps for directions.

Dispute Button: In the worker's attendance history list, add a "Flag" icon next to entries. Tapping it sets the record as disputed.

Localization: Add a settings toggle to switch the app language between English and Hindi. Implement a simple context or dictionary for key UI strings.

Part 3: Contractor Frontend (React Native)

QR Scanner: Add a camera scanner screen (using expo-camera) where the contractor can scan a worker's QR code to instantly mark them "Present" for the day.

Approvals Queue: Add a section on the contractor dashboard displaying pending advance requests with quick "Approve" and "Deny" buttons. Approving must automatically log the transaction in the ledger.

Dispute Highlights: On the contractor's ledger/attendance view, highlight any disputed rows in yellow so they can be resolved on-site.




Phase 7: Day & Night Theme Engine

Theme Context: Implement a React Context provider for theme management (Light Mode / Dark Mode).

Persistence: Use AsyncStorage or Expo SecureStore to save the user's theme preference so it persists across app restarts.

UI Implementation: Add a "Dark Mode" toggle in both the Contractor and Worker Settings tabs. Update the global stylesheet to dynamically switch background colors, text colors, and card surfaces based on the active theme.

Please start by providing the FastAPI models, routes, and Alembic steps for Phase 6. Wait for my confirmation before generating the React Native components.



Phase 8: Payroll Engine, Payment Modes, & Worker Departments
Please implement the following structural and UI updates to support complete payroll management and worker categorization.

Part 1: Backend Schema & API Updates

Worker Departments: Update the users table to include a trade or department column (e.g., Electrician, Plumber, Mason, General Labour).

Payment Modes: Update the transactions or advances table to include a payment_mode column (Enum or String: 'Cash', 'UPI', 'Bank Transfer').

Payroll Settlement Route: Create a POST /payroll/settle endpoint that calculates a worker's total payable amount for a given date range (Total Wages - Total Advances) and creates a final clearing transaction to reset their outstanding balance to ₹0.

Provide the Alembic migration commands for the new trade and payment_mode columns.

Part 2: Frontend Updates (React Native)

Department Badges & Icons: On the Contractor's "Workers" list and "Bulk Attendance" grid, display a small, relevant icon (e.g., ⚡ for Electrician, 🧱 for Mason, 🔨 for Carpenter) and the department name next to each worker's profile so the contractor can instantly identify their role.

Update 'Add Worker' Form: Add a dropdown picker in the 'Add Worker' modal to select the worker's department/trade during creation.

Payment Mode Selector: Whenever the contractor logs a payment or approves an advance, add a toggle/radio button to record how it was paid (Cash, UPI, or Bank Transfer). Display this payment mode in the worker's transaction history.

Payroll/Settlement Screen: Create a "Settle Payroll" button on a worker's detail screen. It should display a summary of their un-settled days and advances, allow the contractor to select the payment mode (e.g., UPI), and confirm the final payout to clear their ledger for that period.




Phase 9: Site Logs, Media Attachments, & Weather Management
Please implement the following features to handle real-world site conditions and media logs.

Part 1: Backend Schema & API Updates

Site Photo Logs: Create a site_photos table (id, site_id, image_url or file_path, date, description, uploaded_by). Create a POST endpoint to handle multipart form data for image uploads, associating the photo with a specific date and site.

Audio Note Logs: Update the advance_requests and transactions tables with an optional audio_note_url column. Create an endpoint to accept and store short audio files.

Weather/Rain-Out Route: Update the attendance status enum/string to accept a 'Rain-Out' or 'Weather' state. Create a POST /attendance/sites/{site_id}/rainout endpoint that instantly logs a 'Rain-Out' status for all active workers assigned to that site for the current date.

Provide the Alembic migration commands for these database changes.

Part 2: Frontend Updates (React Native / Expo)

Daily Photo Feed: On the Site Dashboard, add a "Site Gallery" section. Use expo-image-picker or expo-camera to allow the contractor to snap and upload a photo of the day's progress or material receipts.

Audio Recording UI: Use expo-av to add a small microphone button next to the text input fields in the "Add Advance" and "Dispute" modals. Allow holding to record a short voice note, which uploads alongside the form data.

Rain-Out Button: On the Bulk Attendance screen, add a prominent "Mark Rain-Out" button. When pressed, trigger an alert confirmation ("Mark all workers absent due to weather?") and hit the bulk rain-out API, updating the UI grid immediately.



Phase 10: Advanced Site Logistics, Piece-Rate Payroll, and Asset Tracking
Please implement the following features to complete the robust daily operations suite for the application.

Part 1: Backend Schema & API Updates (FastAPI + SQLAlchemy)

Petty Cash Ledger: Create a petty_cash table (id, site_id, amount, description, date). Create standard POST and GET routes to track non-wage expenses.

Piece-Rate Payroll: Update the users table to include a wage_type column (Enum: 'Daily', 'Piece-Rate'). Create a piece_rate_logs table (id, worker_id, site_id, work_description, quantity, rate, total_amount, date) to track unit-based pay (e.g., sq ft of tiles).

Auto-Installment Deductions: Create an auto_deductions table (id, worker_id, principal_amount, daily_deduction, balance_remaining, is_active). Update the bulk attendance and payroll routes to automatically subtract the daily_deduction from the worker's payable balance if an active deduction exists.

Tool Checkout: Create tools (id, name, inventory_count, site_id) and tool_assignments (id, tool_id, worker_id, checkout_time, return_time, status) tables. Create endpoints to assign and return tools.

Material GRN (Goods Receipt Note): Create site_inventory (id, site_id, item_name, quantity_in_stock, unit) and material_receipts (id, site_id, item_name, quantity_received, challan_image_url, date) tables to update live stock upon delivery.

Migrations: Provide the Alembic commands to autogenerate and apply all of these schema changes.

Part 2: Frontend UI Updates (React Native / Expo)

Site Expenses Screen: Add a "Petty Cash" tab on the Site Dashboard to quickly log miscellaneous cash outflows (e.g., transport fares or daily orders from The Urban Chai Paratha).

Dynamic Wage UI: On the Bulk Attendance grid, if a worker's wage_type is 'Piece-Rate', replace the P/A/H toggle with a quick-entry numeric input for "Quantity" and "Rate".

EMI Toggle: In the "Add Advance" modal, add a switch for "Recover via Daily Installments." If toggled, show an input field for the daily deduction amount.

Tool Assignment Tracker: Build a "Tools" screen where the contractor can assign a drill or grinder to a specific worker. Add a warning badge on the main dashboard if tools remain unreturned at the end of the shift.

Material Ingestion Form: Add a "Receive Material" button to the site dashboard. The modal should require the item name, received quantity (which auto-adds to site_inventory), and prompt the camera to capture a photo of the physical delivery challan.




Phase 11: Business Operations, Access Control, and Reporting
Please implement the final business management layer for the contractor.

Part 1: Backend Schema & API Updates

Role-Based Access Control (RBAC): Update the users table to include a role enum (Admin, Supervisor, Worker). Update the sites table to include a supervisor_id.

Route Protection: Update FastAPI dependency injection to ensure Supervisor roles can only POST attendance and material logs for their assigned site_id, and GET requests for financials are restricted to Admin only.

Machinery & Fuel: Create a machinery_logs table (id, site_id, machine_name, hours_operated, diesel_liters, fuel_cost, date). Create standard CRUD endpoints.

Client Invoices: Create an invoices table (id, site_id, client_name, total_amount, status, pdf_url, created_at).

Data Export Route: Create a GET /export/monthly-report endpoint that queries all transactions, attendance, and petty cash for a given month and returns a downloadable CSV/Excel file using pandas or Python's built-in csv module.

Migrations: Provide the Alembic commands for these schema additions.

Part 2: Frontend UI Updates (React Native / Expo)

Supervisor Dashboard: Implement a conditional render on the main layout. If the logged-in user is a Supervisor, hide the "Total Project Finances" and "Payroll Settlement" tabs, showing only the Bulk Attendance, Tool Checkout, and Material Receipt screens for their assigned site.

Machinery Log Screen: Add a "Machinery" tab to the Site Dashboard where supervisors can log daily equipment usage and upload photos of diesel receipts.

Generate Invoice Button: Add an "Invoicing" screen for the Admin. It should allow them to select a site, input a client name, and auto-calculate a bill based on piece-rate work and materials, generating a PDF layout (using expo-print or a backend PDF generator).

Export Accounting Report: Add an "Export to Excel" button in the Admin Settings. Hitting this triggers the backend export route and uses expo-file-system and expo-sharing to save or WhatsApp the file to their accountant.
