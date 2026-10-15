import { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "../api/client";
import { useAuth } from "../store/auth";
import { t } from "../uiText";

export function Login() {
  const { authenticated, login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? "/";

  if (authenticated) return <Navigate to={from} replace />;

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError("");
    setBusy(true);
    try {
      await login(username.trim(), password);
      navigate(from, { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t.networkError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-page">
      <div className="login-card fade-in">
        <img className="login-logo" src="/icons/icon-512.png" alt="Vallotton" />
        <h1 className="login-title">Vallotton</h1>
        <p className="login-sub">{t.appTagline}</p>
        <form onSubmit={onSubmit}>
          {error && <div className="login-error">{error}</div>}
          <input
            className="login-input"
            placeholder={t.username}
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
          />
          <input
            className="login-input"
            type="password"
            placeholder={t.password}
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <button className="login-submit" type="submit" disabled={busy || !username || !password}>
            {busy ? t.loggingIn : t.login}
          </button>
        </form>
      </div>
    </div>
  );
}
