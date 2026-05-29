"use client";
import { useState } from "react";
import { Shield, Eye, EyeOff, Loader2 } from "lucide-react";
import { api } from "@/lib/api";

interface LoginPageProps {
  onLogin: (token: string) => void;
}

export default function LoginPage({ onLogin }: LoginPageProps) {
  const [password, setPassword] = useState("");
  const [otp, setOtp] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const res = await api.login(password, otp);
      localStorage.setItem("fc_token", res.access_token);
      onLogin(res.access_token);
    } catch (err: any) {
      setError(err.message || "Login failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <div className="w-14 h-14 rounded-2xl bg-brand-600 flex items-center justify-center text-white font-bold text-2xl mx-auto mb-4">
            ₹
          </div>
          <h1 className="text-2xl font-bold">FinCoach</h1>
          <p className="text-sm text-[var(--muted)] mt-1">Smart Finance Ledger</p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="p-6 rounded-2xl bg-[var(--card)] border border-[var(--card-border)] space-y-4"
        >
          <div className="flex items-center gap-2 text-xs text-[var(--muted)] mb-2">
            <Shield className="w-3.5 h-3.5" />
            <span>Admin Access Only</span>
          </div>

          {error && (
            <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-xs">
              {error}
            </div>
          )}

          <div>
            <label className="text-xs font-medium text-[var(--muted)] mb-1.5 block">
              Password
            </label>
            <div className="relative">
              <input
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full px-3 py-2.5 rounded-lg bg-[var(--background)] border border-[var(--card-border)] text-sm focus:outline-none focus:ring-2 focus:ring-brand-600/50 pr-10"
                placeholder="Enter admin password"
                required
                autoFocus
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-[var(--muted)] hover:text-white"
              >
                {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
          </div>

          <div>
            <label className="text-xs font-medium text-[var(--muted)] mb-1.5 block">
              OTP Code
            </label>
            <input
              type="text"
              value={otp}
              onChange={(e) => setOtp(e.target.value.replace(/\D/g, "").slice(0, 6))}
              className="w-full px-3 py-2.5 rounded-lg bg-[var(--background)] border border-[var(--card-border)] text-sm focus:outline-none focus:ring-2 focus:ring-brand-600/50 tracking-[0.3em] text-center font-mono"
              placeholder="••••••"
              maxLength={6}
              required
            />
          </div>

          <button
            type="submit"
            disabled={loading || !password || otp.length < 4}
            className="w-full py-2.5 rounded-lg bg-brand-600 text-white text-sm font-medium hover:bg-brand-700 disabled:opacity-50 disabled:cursor-not-allowed transition-all flex items-center justify-center gap-2"
          >
            {loading ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                Verifying...
              </>
            ) : (
              "Login"
            )}
          </button>
        </form>
      </div>
    </div>
  );
}

