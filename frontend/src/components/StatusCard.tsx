import { Card, CardContent } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"

type Props = { message: string; progress: number; error?: string | null; interrupted?: boolean; onRetry?: () => void; retrying?: boolean }

export function StatusCard({ message, progress, error, interrupted, onRetry, retrying }: Props) {
  return (
    <Card>
      <CardContent className="space-y-3 pt-6">
        <div role="status" aria-live="polite" className="font-medium">{error ? (interrupted ? "Interrupted" : "Failed") : message}</div>
        {!error && <Progress value={Math.round(progress * 100)} aria-label="Progress" />}
        {error && (
          <Alert variant="destructive">
            <AlertTitle>{interrupted ? "The server restarted during this job" : "Something went wrong"}</AlertTitle>
            <AlertDescription className="whitespace-pre-line">{error}</AlertDescription>
          </Alert>
        )}
        {error && onRetry && (
          <Button variant="outline" onClick={onRetry} disabled={retrying}>{retrying ? "Retrying..." : "Retry with the stored inputs"}</Button>
        )}
      </CardContent>
    </Card>
  )
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <Alert variant="destructive" role="alert">
      <AlertDescription className="whitespace-pre-line">{message}</AlertDescription>
    </Alert>
  )
}

export function WarnNote({ message }: { message: string }) {
  return (
    <Alert>
      <AlertDescription className="whitespace-pre-line">{message}</AlertDescription>
    </Alert>
  )
}

export const when = (unixSeconds: number) => new Date(unixSeconds * 1000).toLocaleString()
