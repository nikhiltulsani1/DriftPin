TITLE:        Voice Assistant — Quick Capture FAB
VERSION:      v1.0
STAKEHOLDER:  Product — MyPact Core
PRIORITY:     P0

## Overview
A globally mounted floating action button lets users log meals, mood, workouts,
schedule block completions, and quick notes entirely by voice. The AI (Groq
llama-3.3-70b-versatile) parses intent and routes to the correct service.
A TTS confirmation reply is spoken after each action.

## Requirements

R-01: Voice input captured via device-native STT (`@react-native-voice/voice`).
R-02: AI parses intent from transcribed text and routes to exactly one of five
      services: meal log, mood check-in, workout log, block completion,
      or quick note (fallback).
R-03: Unrecognised or ambiguous input routes to quick note — never silently dropped.
R-04: AI speaks a confirmation reply via TTS (`expo-speech`) after every action.
R-05: FAB is hidden on the Schedule screen.
R-06: FAB closes automatically 2.8 seconds after a successful action.
R-07: Text mode input available as fallback — saves as quick note only.
R-08: Voice-captured notes are auto-tagged `voice` in the `notes` table.
R-09: Meal log: AI estimates calories from free-text food description; result
      stored in `meal_logs`.
R-10: Block completion: AI matches "finished my [X]" to an active or recent
      schedule block in `block_completions`.
R-11: Mood log: sentiment phrasing ("feeling great", "tired today") maps to
      a 1–5 scale value stored in `daily_checkins`.
R-12: Workout log: activity type and duration extracted from speech and stored
      in `workout_logs`.

## Acceptance Criteria

AC-01 (R-02): "Had dal rice for lunch" → meal_logs entry, category Lunch.
AC-02 (R-02): "Feeling great today" → daily_checkins mood update.
AC-03 (R-02): "Did 30 minutes of yoga" → workout_logs entry, type Yoga, duration 30.
AC-04 (R-02): "Finished my morning study" → block_completions entry for closest
              matching active block.
AC-05 (R-03): "banana" (ambiguous, no intent signal) → notes entry, tagged `voice`.
AC-06 (R-03): Empty speech / mic timeout → no entry created, no crash, FAB resets.
AC-07 (R-04): Every successful action triggers a spoken TTS confirmation.
AC-08 (R-04): TTS confirmation does not play if device is on silent (system respects
              device audio state).
AC-09 (R-05): FAB component does not render on Schedule screen.
AC-10 (R-06): FAB auto-dismisses 2.8s (±0.3s) after successful action completion.
AC-11 (R-09): Calorie estimate for "2 rotis dal sabzi" is stored as a numeric value
              (not null, not zero, not a string).
AC-12 (R-10): If no active block matches the description, action saves as a note,
              NOT silently dropped.
AC-13 (R-11): "Feeling great" maps to mood ≥ 4; "exhausted" maps to mood ≤ 2.
AC-14 (R-12): "Ran for an hour" → duration = 60, type = Run.

## Out of Scope
- Multi-action in a single utterance ("had lunch and finished yoga")
- Language support other than English
- Offline AI intent parsing
- Voice commands for schedule creation or deletion

## Dependencies
- Groq API available and responding within 3s p50
- `@react-native-voice/voice` microphone permission granted
- Active Supabase session (authenticated user)
- `expo-speech` available on device

## Non-Functional Requirements
- End-to-end voice flow (tap → speak → confirm) < 3s on Groq p50
- FAB available on all tabs except Schedule — no exceptions
- Crash-free on STT failure or network timeout
