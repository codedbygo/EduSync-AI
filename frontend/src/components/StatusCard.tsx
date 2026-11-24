import { Card, CardContent } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"

export function StatusCard({ message, progress, error }: { message: string; progress: number; error?: string | null }) {
  return (
    <Card>
      <CardContent className="space-y-3 pt-6">
        <div role="status" aria-live="polite" className="font-medium">{error ? "Failed" : message}</div>
        {!error && <Progress value={Math.round(progress * 100)} aria-label="Progress" />}
        {error && (
          <Alert variant="destructive">
            <AlertTitle>Something went wrong</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
      </CardContent>
    </Card>
  )
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <Alert variant="destructive" role="alert">
      <AlertDescription>{message}</AlertDescription>
    </Alert>
  )
}
