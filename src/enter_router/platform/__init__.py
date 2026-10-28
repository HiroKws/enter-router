"""Windows adapters. NEVER imported by core/ or by Linux tests.

Every module here isolates one Win32 concern behind a narrow function
interface so the pure core stays OS-free and the Windows code stays
trivially reviewable. All hard-won constraints from the research card
(research.md §2, §附録) are repeated in the docstrings where they bite.
"""
