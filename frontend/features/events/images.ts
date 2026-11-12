// Event banner images: the Event Service accepts JPEG, PNG and WebP files up to 300 KB.
export const maxImageBytes = 300 * 1024;
export const imageTypes = ["image/jpeg", "image/png", "image/webp"];

// A message for a file the Event Service would reject, or undefined when it is fine.
export function imageProblem(file: File) {
  if (!imageTypes.includes(file.type))
    return "Choose a JPEG, PNG or WebP image.";
  if (file.size > maxImageBytes)
    return `The image is ${Math.ceil(file.size / 1024)} KB. Choose one that is ${maxImageBytes / 1024} KB or smaller.`;
  return undefined;
}
