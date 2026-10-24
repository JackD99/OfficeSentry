-- Adding clients from Partner Center (partner.py): the MSP's GDAP customers, read with the setup wizard's device
-- sign-in (purpose 'partner'), are kept on the admin's sign-in row as JSON until they add the ones they tick. No
-- token or secret is in it, only customer names, tenant IDs and relationship details, and the row goes after a day.
ALTER TABLE setup_sign_ins ADD COLUMN found TEXT;
