-- What a client's copy of a month carried when it was approved (month_end.freeze): the commentary,
-- recommendations, client-visible notes and what the reviewer left out, as JSON. The client's live report
-- shows this copy, so a change made after approval waits until the month is approved again (a re-issue).
-- NULL for a month approved before this column existed, or sent by an automatic plan: those show the
-- month's text as it is now, as before.
ALTER TABLE report_reviews ADD COLUMN approved_copy TEXT;
