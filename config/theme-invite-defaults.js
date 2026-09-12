// Pre-fill the invite form's "max uses" with 1.
//
// There is no site setting that reaches this value. Discourse computes the
// pre-filled number in the frontend as min(ceiling, 100) for staff and
// min(ceiling, 10) for everyone else, where the ceiling is
// invite_link_max_redemptions_limit or invite_link_max_redemptions_limit_users.
// So the only lever the settings expose is the ceiling, and the ceilings are
// deliberately left at their shipped values: a staff member who wants a
// multi-use link should still be able to type one. Only the default changes.
//
// Both modal variants are patched, because enable_invite_modal_with_roles
// chooses between them at runtime and the getter is identical in each.
//
// This is a monkey-patch of a core getter, and it fails soft. If a future
// Discourse renames or removes defaultRedemptionsAllowed, the form goes back to
// pre-filling 100 and nothing breaks; the warning below is how that surfaces.
// docs/05-forum-config.md says how to check which of the two is happening.
//
// It lives in a theme component rather than in the container, because a theme
// is stored in the database and survives ./launcher rebuild. An edit to the
// image's source does not.

import CreateInvite from "discourse/components/modal/create-invite";
import CreateInviteWithRoles from "discourse/components/modal/create-invite-with-roles";
import { apiInitializer } from "discourse/lib/api";

const PREFILLED_MAX_USES = 1;

export default apiInitializer(() => {
  for (const modal of [CreateInvite, CreateInviteWithRoles]) {
    if (!("defaultRedemptionsAllowed" in modal.prototype)) {
      // eslint-disable-next-line no-console
      console.warn(
        `qupa-cafe: ${modal.name} has no defaultRedemptionsAllowed any more, ` +
          "so the invite form will pre-fill Discourse's own default again."
      );
      continue;
    }

    Object.defineProperty(modal.prototype, "defaultRedemptionsAllowed", {
      configurable: true,
      get() {
        return PREFILLED_MAX_USES;
      },
    });
  }
});
