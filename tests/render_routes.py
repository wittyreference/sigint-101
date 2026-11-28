# ABOUTME: The pages the render checks load on the console they measure, as path and query.
# ABOUTME: The GET guard walks them (test_app.py); test_render_checks.py proves the list is whole.

# Kept whole by test_every_page_the_real_checks_load_is_walked, which runs the real checks under
# the runner and reads back every page they navigate to. Recall's own scratch console pages
# are listed too (/lesson/1, /lesson/1?recall=card&card=2), although it never loads them from
# the measured console.
RENDER_CHECK_PAGES = ("/lesson/29", "/", "/exam/review/T6C02", "/operate/ism", "/logbook",
                      "/operate/lora", "/lesson/5", "/law?back=/lesson/5", "/lesson/39",
                      "/lesson/1", "/lesson/1?recall=card&card=2", "/unit/4", "/unit/5",
                      "/unit/4/challenge", "/unit/5/challenge",
                      "/operate/ism?back=/unit/4/challenge", "/operate/waterfall",
                      "/field", "/bands", "/lesson/40", "/lesson/26")
