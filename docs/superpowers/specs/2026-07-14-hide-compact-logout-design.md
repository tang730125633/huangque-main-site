# Hide Compact Sidebar Logout Design

## Goal

Remove the small logout control overlaid on the avatar when the desktop sidebar is in compact icon-only mode.

## Behavior

- Desktop pages other than Inspiration keep the compact 68px sidebar.
- Compact mode hides the sidebar logout control completely.
- Inspiration keeps the expanded sidebar and its existing logout control.
- Narrow viewports keep the expanded drawer behavior and its existing logout control.
- Navigation labels, hover/focus tooltips, and account identity remain unchanged.

## Implementation

- Remove the compact-mode CSS override that forces `button.hq-user-logout` to display.
- Keep the existing default compact rule that hides `.hq-user-logout`.
- Update the sidebar regression test to assert the compact override is absent while the expanded logout button still exists.
- Regenerate every workbench HTML cache stamp for `cloud-shell.js`.

## Verification

- Run the sidebar Node regression suite.
- Run `node --check site/workbench/cloud-shell.js`.
- Verify every workbench HTML references the newly calculated `cloud-shell.js` stamp.
- Confirm the local `8788` response matches the edited files.

## Out Of Scope

- Removing logout from the expanded sidebar.
- Adding a replacement account menu to the top bar.
- Changing navigation layout, labels, icons, or mobile behavior.
