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

      if (!email || !email.includes("@")) {
        return new Response(JSON.stringify({ error: "Invalid email format" }), { status: 400, headers: corsHeaders });
      }

      // Helper: Robust Base64 (Handles Unicode to prevent 500 errors on special chars)
      const safeBtoa = (str) => btoa(unescape(encodeURIComponent(str || "")));

      // 3. Robust Domain Handling
      let zDomain = env.ZITADEL_DOMAIN.replace(/^https?:\/\//, "").replace(/\/$/, "");
      const zitadelUrl = `https://${zDomain}/v2/users/human`;

      // 4. Create User in Zitadel
      const payload = {
        email: { email: email, isEmailVerified: true },
        profile: { givenName: "HomeBrain", familyName: "User", displayName: email },
        metadata: [
          { key: "device_id", value: safeBtoa(device_id || "unknown") },
          { key: "registration_source", value: safeBtoa("homebrain_manager") }
        ]
      };

      // ROBUSTNESS: Use AbortController instead of AbortSignal.timeout (compatibility)
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 8000);

      const zResponse = await fetch(zitadelUrl, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.ZITADEL_PAT}`,
          "Content-Type": "application/json",
          "x-zitadel-orgid": env.ZITADEL_ORG_ID
        },
        body: JSON.stringify(payload),
        signal: controller.signal
      });

      clearTimeout(timeoutId); // Clear timeout immediately after fetch

      if (zResponse.ok) {
        const userData = await zResponse.json();
        const userId = userData.userId;

        // --- User Grant ---
        // Use /grants instead of /members. 'Members' implies admin/management rights on the project.
        // 'Grants' implies "permission to use the application".
        const grantUrl = `https://${zDomain}/management/v1/users/${userId}/grants`;

        const grantResponse = await fetch(grantUrl, {
            method: "POST",
            headers: {
                "Authorization": `Bearer ${env.ZITADEL_PAT}`,
                "Content-Type": "application/json",
                "x-zitadel-orgid": env.ZITADEL_ORG_ID
            },
            body: JSON.stringify({
                projectId: env.ZITADEL_PROJECT_ID,
                roleKeys: [env.ZITADEL_PROJECT_ROLE || "user"] 
            })
        });

        // --- Trigger Initialization Email ---
        // Essential: Since we verify the email implicitly, we must send this link
        // so the user can set their password.
        const initUrl = `https://${zDomain}/management/v1/users/${userId}/initialization_email`;
        await fetch(initUrl, {
            method: "POST",
            headers: {
                "Authorization": `Bearer ${env.ZITADEL_PAT}`,
                "Content-Type": "application/json",
                "x-zitadel-orgid": env.ZITADEL_ORG_ID
            },
            body: JSON.stringify({}) 
        });

        if (!grantResponse.ok) {
            // Log error but don't fail the whole request since user is created
            const errText = await grantResponse.text();
            console.error("Failed to assign project grant:", errText);
        }

        return new Response(JSON.stringify({ status: "success", message: "Invitation sent" }), {
          status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      } else {
        const zText = await zResponse.text();
        let zError;
        try { zError = JSON.parse(zText); } catch(e) { zError = { message: zText }; }

        // Code 6 = Already Exists
        if (zResponse.status === 409 || (zError.code === 6)) {
           return new Response(JSON.stringify({ status: "success", message: "User already registered" }), {
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
