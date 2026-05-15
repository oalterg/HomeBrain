/**
 * HomeBrain Registrar Service
 * Handles secure user registration requests from HomeBrain devices.
 * * Environment Variables required:
 * - ZITADEL_DOMAIN: e.g. "auth.yourdomain.com"
 * - ZITADEL_ORG_ID: Your Zitadel Organization ID
 * - ZITADEL_PAT: Personal Access Token for the Service User
 * - REGISTRAR_SECRET: Shared secret key for authenticating requests
 */
export default {
  async fetch(request, env) {
    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
    };

    if (request.method === "OPTIONS") return new Response(null, { headers: corsHeaders });
    if (request.method !== "POST") return new Response("Method not allowed", { status: 405, headers: corsHeaders });

    try {
      // 1. Validate Env
      if (!env.ZITADEL_DOMAIN || !env.ZITADEL_ORG_ID || !env.ZITADEL_PROJECT_ID || !env.ZITADEL_PAT || !env.REGISTRAR_SECRET) {
        throw new Error("Worker misconfigured: Missing environment variables.");
      }

      // 2. Security Check
      const authHeader = request.headers.get("Authorization");
      if (!authHeader || authHeader.trim() !== `Bearer ${env.REGISTRAR_SECRET}`) {
         return new Response(JSON.stringify({ error: "Unauthorized: Invalid Secret" }), {
           status: 401, headers: { ...corsHeaders, "Content-Type": "application/json" }
         });
      }

      // Robust request parsing to prevent JSON syntax errors
      let email, device_id;
      try {
        const body = await request.json();
        email = body.email;
        device_id = body.device_id;
      } catch (e) {
        return new Response(JSON.stringify({ error: "Invalid JSON body" }), { status: 400, headers: corsHeaders });
      }

      if (!email || !email.includes("@")) {
        return new Response(JSON.stringify({ error: "Invalid email format" }), { status: 400, headers: corsHeaders });
      }

      // Helper: Robust Base64 (Handles Unicode to prevent 500 errors on special chars)
      const safeBtoa = (str) => btoa(unescape(encodeURIComponent(str || "")));

      // 3. Robust Domain Handling
      const zDomain = env.ZITADEL_DOMAIN.replace(/^https?:\/\//, "").replace(/\/$/, "");
      const zHeaders = {
        "Authorization": `Bearer ${env.ZITADEL_PAT}`,
        "Content-Type": "application/json",
        "x-zitadel-orgid": env.ZITADEL_ORG_ID
      };

      // Wrap fetch with an 8s timeout via AbortController (broader compat than
      // AbortSignal.timeout). Each call gets its own controller.
      const zFetch = async (url, init) => {
        const controller = new AbortController();
        const t = setTimeout(() => controller.abort(), 8000);
        try {
          return await fetch(url, { ...init, signal: controller.signal });
        } finally {
          clearTimeout(t);
        }
      };

      // Send the password-setup link to the freshly created user. Returns an
      // {ok, error} object; callers decide whether to surface failures or
      // treat them as soft.
      const sendInitEmail = async (userId) => {
        const initUrl = `https://${zDomain}/management/v1/users/${userId}/initialization_email`;
        const r = await zFetch(initUrl, { method: "POST", headers: zHeaders, body: JSON.stringify({}) });
        if (r.ok) return { ok: true };
        const text = await r.text().catch(() => "");
        console.error(`initialization_email failed for ${userId}: ${r.status} ${text}`);
        return { ok: false, status: r.status, error: text };
      };

      // 4. Create User in Zitadel
      const payload = {
        email: { email: email, isEmailVerified: true },
        profile: { givenName: "HomeBrain", familyName: "User", displayName: email },
        metadata: [
          { key: "device_id", value: safeBtoa(device_id || "unknown") },
          { key: "registration_source", value: safeBtoa("homebrain_manager") }
        ]
      };

      const zResponse = await zFetch(`https://${zDomain}/v2/users/human`, {
        method: "POST", headers: zHeaders, body: JSON.stringify(payload)
      });

      if (zResponse.ok) {
        const userData = await zResponse.json();
        const userId = userData.userId;

        // --- User Grant ---
        // Use /grants instead of /members. 'Members' implies admin/management rights on the project.
        // 'Grants' implies "permission to use the application".
        const grantResponse = await zFetch(`https://${zDomain}/management/v1/users/${userId}/grants`, {
          method: "POST", headers: zHeaders,
          body: JSON.stringify({
            projectId: env.ZITADEL_PROJECT_ID,
            roleKeys: [env.ZITADEL_PROJECT_ROLE || "user"]
          })
        });
        if (!grantResponse.ok) {
          // Log error but don't fail the whole request since user is created
          const errText = await grantResponse.text().catch(() => "");
          console.error(`Failed to assign project grant for ${userId}: ${errText}`);
        }

        // --- Trigger Initialization Email ---
        // Required: we marked the address as verified above, so Zitadel will
        // not auto-send a verification mail. The user gets the password-setup
        // link only from this call — if it fails (SMTP down, mailbox blocks,
        // etc.) the user is silently stranded. Surface it to the caller.
        const initResult = await sendInitEmail(userId);
        if (!initResult.ok) {
          return new Response(JSON.stringify({
            error: `User created but invitation email could not be sent: ${initResult.error || "unknown error"}`,
            user_created: true
          }), { status: 502, headers: { ...corsHeaders, "Content-Type": "application/json" } });
        }

        console.log(`Registered new user ${userId} (${email}) for device ${device_id || "unknown"}`);
        return new Response(JSON.stringify({ status: "success", message: "Invitation sent" }), {
          status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      } else {
        const zText = await zResponse.text().catch(() => "");
        let zError;
        try { zError = JSON.parse(zText); } catch(e) { zError = { message: zText }; }

        // Code 6 = Already Exists. Re-trigger the init email so a user who
        // missed the first one (typo in address fixed via Zitadel admin,
        // greylisted message, lost-in-spam, etc.) can request a fresh link
        // by re-submitting on the device. Without this branch the worker
        // would silently return success without sending anything — exactly
        // the failure mode that masked the protomail/protonmail typo above.
        if (zResponse.status === 409 || (zError.code === 6)) {
          const lookup = await zFetch(`https://${zDomain}/v2/users`, {
            method: "POST", headers: zHeaders,
            body: JSON.stringify({
              queries: [{ emailQuery: { emailAddress: email, method: "TEXT_QUERY_METHOD_EQUALS" } }]
            })
          });
          if (!lookup.ok) {
            const errText = await lookup.text().catch(() => "");
            console.error(`Lookup failed for existing user ${email}: ${lookup.status} ${errText}`);
            // Fall through to the old success-without-resend shape — the user
            // does exist, we just couldn't re-trigger. Better than 500.
            return new Response(JSON.stringify({ status: "success", message: "User already registered" }), {
              status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" }
            });
          }
          const lookupBody = await lookup.json().catch(() => ({}));
          const existingId = (lookupBody.result || [])[0]?.userId;
          if (!existingId) {
            console.error(`Existing user search returned no result for ${email}`);
            return new Response(JSON.stringify({ status: "success", message: "User already registered" }), {
              status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" }
            });
          }
          const resend = await sendInitEmail(existingId);
          if (!resend.ok) {
            return new Response(JSON.stringify({
              error: `User exists but resending invitation failed: ${resend.error || "unknown error"}`
            }), { status: 502, headers: { ...corsHeaders, "Content-Type": "application/json" } });
          }
          console.log(`Re-sent invitation to existing user ${existingId} (${email})`);
          return new Response(JSON.stringify({ status: "success", message: "Invitation re-sent" }), {
            status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" }
          });
        }

        return new Response(JSON.stringify({ error: `Zitadel Provider Error: ${zError.message || zText}` }), {
          status: 502, headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      }
    } catch (e) {
      return new Response(JSON.stringify({ error: `Worker Exception: ${e.message}` }), {
        status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" }
      });
    }
  }
};
