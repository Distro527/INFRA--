// Client-side Firebase Auth integration
// Supports: Google sign-in, email/password (sign in & register), password reset, magic link sign-in

import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js";
import {
  getAuth,
  signInWithPopup,
  GoogleAuthProvider,
  OAuthProvider,
  GithubAuthProvider,
  signInWithEmailAndPassword,
  createUserWithEmailAndPassword,
  sendPasswordResetEmail,
  sendSignInLinkToEmail,
  isSignInWithEmailLink,
  signInWithEmailLink,
} from "https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js";

const firebaseConfig = window.FIREBASE_CONFIG || {};
const app = initializeApp(firebaseConfig);
const auth = getAuth(app);

// Configure action code settings for magic link
const actionCodeSettings = {
  url: window.location.origin + "/login", // the same page will complete the sign-in
  handleCodeInApp: true,
};

// Basic email validator
function isValidEmail(email) {
  if (!email) return false;
  const e = String(email).trim();
  // Simple RFC 5322-like check (good enough for client-side)
  return /[^@\s]+@[^@\s]+\.[^@\s]+/.test(e);
}

async function exchangeIdTokenForSession(idToken) {
  const res = await fetch("/sessionLogin", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ idToken }),
  });
  if (!res.ok) throw new Error("Failed to create session");
}

function showError(msg) {
  const el = document.getElementById("errorMsg");
  if (el) {
    el.textContent = msg;
    el.classList.remove("hidden");
  } else {
    alert(msg);
  }
}

function hideError() {
  const el = document.getElementById("errorMsg");
  if (el) el.classList.add("hidden");
}

// Google sign-in
const googleBtn = document.getElementById("googleSignIn");
if (googleBtn) {
  googleBtn.addEventListener("click", async () => {
    hideError();
    try {
      const provider = new GoogleAuthProvider();
      const result = await signInWithPopup(auth, provider);
      const idToken = await result.user.getIdToken();
      await exchangeIdTokenForSession(idToken);
      window.location.href = "/dashboard";
    } catch (e) {
      console.error(e);
      showError(e?.message || "Google sign-in failed");
    }
  });
}

// GitHub sign-in
const ghBtn = document.getElementById("githubSignIn");
if (ghBtn) {
  ghBtn.addEventListener("click", async () => {
    hideError();
    try {
      const provider = new GithubAuthProvider();
      // Optional: scope example -> provider.addScope('repo');
      const result = await signInWithPopup(auth, provider);
      const idToken = await result.user.getIdToken();
      await exchangeIdTokenForSession(idToken);
      window.location.href = "/dashboard";
    } catch (e) {
      console.error(e);
      showError(e?.message || "GitHub sign-in failed");
    }
  });
}

// Email/password sign-in & register
const emailForm = document.getElementById("emailForm");
const registerBtn = document.getElementById("register");
if (emailForm) {
  emailForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    hideError();
    const email = document.getElementById("email").value.trim();
    const password = document.getElementById("password").value;
    try {
      if (!isValidEmail(email)) {
        showError("Please enter a valid email address.");
        return;
      }
      const { user } = await signInWithEmailAndPassword(auth, email, password);
      const idToken = await user.getIdToken();
      await exchangeIdTokenForSession(idToken);
      window.location.href = "/dashboard";
    } catch (e) {
      console.error(e);
      showError(e?.message || "Sign-in failed");
    }
  });
}
if (registerBtn) {
  registerBtn.addEventListener("click", async () => {
    hideError();
    const email = document.getElementById("email").value.trim();
    const password = document.getElementById("password").value;
    try {
      if (!isValidEmail(email)) {
        showError("Please enter a valid email address.");
        return;
      }
      const { user } = await createUserWithEmailAndPassword(auth, email, password);
      const idToken = await user.getIdToken();
      await exchangeIdTokenForSession(idToken);
      window.location.href = "/dashboard";
    } catch (e) {
      console.error(e);
      showError(e?.message || "Registration failed");
    }
  });
}

// Microsoft sign-in (Azure AD via Firebase OAuthProvider)
const msBtn = document.getElementById("microsoftSignIn");
if (msBtn) {
  msBtn.addEventListener("click", async () => {
    hideError();
    try {
      const provider = new OAuthProvider('microsoft.com');
      // Optional: request additional scopes
      // provider.addScope('User.Read');
      const result = await signInWithPopup(auth, provider);
      const idToken = await result.user.getIdToken();
      await exchangeIdTokenForSession(idToken);
      window.location.href = "/dashboard";
    } catch (e) {
      console.error(e);
      showError(e?.message || "Microsoft sign-in failed");
    }
  });
}

// Password reset
const resetBtn = document.getElementById("resetBtn");
if (resetBtn) {
  resetBtn.addEventListener("click", async () => {
    hideError();
    const email = document.getElementById("email").value.trim();
    if (!isValidEmail(email)) return showError("Enter a valid email first");
    try {
      await sendPasswordResetEmail(auth, email, actionCodeSettings);
      alert("Password reset email sent. Check your inbox.");
    } catch (e) {
      console.error(e);
      showError(e?.message || "Failed to send reset email");
    }
  });
}

// Magic link (email link) send & complete
const magicLinkBtn = document.getElementById("magicLinkBtn");
if (magicLinkBtn) {
  magicLinkBtn.addEventListener("click", async () => {
    hideError();
    const email = (document.getElementById("magicEmail")?.value || document.getElementById("email")?.value || "").trim();
    if (!isValidEmail(email)) return showError("Enter a valid email for magic link");
    try {
      window.localStorage.setItem("voidsyn_emailForSignIn", email);
      await sendSignInLinkToEmail(auth, email, actionCodeSettings);
      alert("Magic sign-in link sent. Check your email.");
    } catch (e) {
      console.error(e);
      showError(e?.message || "Failed to send magic link");
    }
  });
}

// Complete sign-in with email link if present
(async function handleEmailLinkCompletion() {
  try {
    if (isSignInWithEmailLink(auth, window.location.href)) {
      let email = window.localStorage.getItem("voidsyn_emailForSignIn");
      if (!email) {
        email = window.prompt("Confirm your email for sign-in");
      }
      if (!isValidEmail(email)) throw new Error("Invalid email provided for link sign-in.");
      const result = await signInWithEmailLink(auth, email, window.location.href);
      window.localStorage.removeItem("voidsyn_emailForSignIn");
      const idToken = await result.user.getIdToken();
      await exchangeIdTokenForSession(idToken);
      // Clean URL
      const url = new URL(window.location.href);
      url.search = "";
      window.history.replaceState({}, document.title, url.toString());
      window.location.href = "/dashboard";
    }
  } catch (e) {
    // Non-fatal; show error on login page
    console.error(e);
    if (window.location.pathname === "/login") {
      showError(e?.message || "Failed to complete email link sign-in");
    }
  }
})();

// Logout
const logoutBtn = document.getElementById("logoutBtn");
if (logoutBtn) {
  logoutBtn.addEventListener("click", async () => {
    try {
      await fetch("/sessionLogout", { method: "POST" });
      await auth.signOut();
      window.location.href = "/";
    } catch (e) {
      console.error(e);
      showError("Failed to logout");
    }
  });
}
