"""Two-way Telegram bot: the review and approval flow for tailored resumes.

`callbacks` encodes inline-button data, `client` talks to the Bot API,
`messages` formats every text and keyboard, and `service` polls for updates,
authorizes users, and runs the tailor / regenerate / approve / reject handlers.
"""
